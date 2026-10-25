"""Pure Deployment helpers shared by the agent and the AI gateway.

The admission webhook, the image update endpoint and the AI gateway must apply
the same rules to a Deployment; keeping one copy here stops them drifting apart.
"""

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterator, Optional, Tuple


JVM_FLAGS = {
    "jvm_xms_bytes": "-Xms",
    "jvm_xmx_bytes": "-Xmx",
    "jvm_xss_bytes": "-Xss",
    "jvm_max_metaspace_bytes": "-XX:MaxMetaspaceSize=",
}
JVM_FIELDS = tuple(JVM_FLAGS)
MAX_BIGINT = (1 << 63) - 1
_SIZE_RE = re.compile(r"([0-9]+)([kKmMgG]?)\Z")
_OPTION_OPERANDS = {
    "-cp", "-classpath", "--class-path", "-p", "--module-path",
    "--upgrade-module-path", "--add-modules", "--limit-modules",
    "--add-reads", "--add-exports", "--add-opens", "--patch-module",
    "--enable-native-access", "--source",
}


def parse_jvm_size(value: str) -> Optional[int]:
    """Convert an integer Java size to exact bytes that fit a PostgreSQL bigint."""
    match = _SIZE_RE.fullmatch(value)
    if not match:
        return None
    try:
        amount = int(match.group(1))
    except ValueError:
        return None
    exponent = {"": 0, "k": 1, "m": 2, "g": 3}[match.group(2).lower()]
    size = amount * (1024 ** exponent)
    return size if size <= MAX_BIGINT else None


def format_jvm_size(size: int, unit: str = "m") -> str:
    """Prefer the field's unit without rounding; Java options reject decimals."""
    divisor = 1024 if unit == "k" else 1024 * 1024
    if size % divisor == 0:
        return f"{size // divisor}{unit}"
    if unit == "m" and size % 1024 == 0:
        return f"{size // 1024}k"
    return str(size)


def java_options(args: Any) -> Iterator[Tuple[int, str, str]]:
    """Yield (index, field, size) for each supported launcher option in container args."""
    # Only the agreed Kubernetes args form is supported, not shell/env expansion.
    if not isinstance(args, list) or not args or args[0] != "java":
        return
    index = 1
    while index < len(args):
        argument = args[index]
        if not isinstance(argument, str):
            return
        if argument in {"-jar", "-m", "--module", "--"} or argument.startswith("--module="):
            return
        # The first non-option is the main class/source file; the rest belongs to the app.
        if not argument.startswith("-"):
            return
        if argument in _OPTION_OPERANDS:
            index += 2
            continue
        for field, prefix in JVM_FLAGS.items():
            if argument.startswith(prefix):
                suffix = argument[len(prefix):]
                yield index, field, suffix
                break
        index += 1


def extract_jvm_config(args: Any) -> Dict[str, Optional[int]]:
    """Extract the last occurrence of each supported launcher option."""
    config = dict.fromkeys(JVM_FIELDS)
    for _, field, size in java_options(args):
        # An invalid/overflowing final value must not leave an earlier value in place.
        config[field] = parse_jvm_size(size)
    return config


def replace_jvm_flags(args: Any, config: Dict[str, Any]) -> Any:
    """Replace existing named options only, preserving every other argument."""
    if not isinstance(args, list):
        return args
    result = list(args)
    for index, field, original_size in java_options(args):
        size = config.get(field)
        if _SIZE_RE.fullmatch(original_size) and type(size) is int and 0 <= size <= MAX_BIGINT:
            unit = "k" if field == "jvm_xss_bytes" else "m"
            result[index] = JVM_FLAGS[field] + format_jvm_size(size, unit)
    return result


def replaceable_jvm_fields(args: Any) -> set:
    """Fields replace_jvm_flags can change: present with an integer size in the java args."""
    return {field for _, field, size in java_options(args) if _SIZE_RE.fullmatch(size)}


def replace_image_tag(image: str, tag: str) -> str:
    """Swap only the tag. Registries may carry a port (harbor:5000/app:v1), images a digest."""
    name = image.split("@", 1)[0]
    if name.rfind(":") > name.rfind("/"):
        name = name[:name.rfind(":")]
    return f"{name}:{tag}"


_QUANTITY_RE = re.compile(r"([+]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)\Z")
_BINARY = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60}
_DECIMAL = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"), "k": 10 ** 3,
            "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18}


def _quantity(value: Any) -> Optional[Decimal]:
    """A Kubernetes resource quantity as a Decimal of base units; None if absent or invalid."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value)) if math.isfinite(value) and value >= 0 else None
    match = _QUANTITY_RE.fullmatch(str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    scale = _BINARY.get(suffix, _DECIMAL.get(suffix))
    if scale is None:
        return None
    try:
        return Decimal(number) * scale
    except InvalidOperation:
        return None


def parse_cpu_millicores(value: Any) -> Optional[int]:
    """CPU quantity ("500m", "0.5", "2") to millicores, rounded up."""
    amount = _quantity(value)
    return None if amount is None else math.ceil(amount * 1000)


def parse_memory_mib(value: Any) -> Optional[int]:
    """Memory quantity ("512Mi", "1Gi", "1G", bytes) to MiB, rounded up."""
    amount = _quantity(value)
    return None if amount is None else math.ceil(amount / 2 ** 20)
