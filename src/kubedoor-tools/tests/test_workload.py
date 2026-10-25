"""Pure Deployment helpers shared by the agent webhook and the AI gateway."""

import pytest

from kubedoor_tools.workload import (
    extract_jvm_config,
    parse_cpu_millicores,
    parse_memory_mib,
    replace_image_tag,
    replace_jvm_flags,
    replaceable_jvm_fields,
)


@pytest.mark.parametrize("image,expected", [
    ("app:v1", "app:v2"),
    ("app", "app:v2"),
    ("registry.example.com/team/app:v1", "registry.example.com/team/app:v2"),
    # A registry port must survive: the old split(':')[0] produced "harbor.example.com:v2".
    ("harbor.example.com:5000/team/app:v1", "harbor.example.com:5000/team/app:v2"),
    ("harbor.example.com:5000/team/app", "harbor.example.com:5000/team/app:v2"),
    ("app:v1@sha256:" + "a" * 64, "app:v2"),
    ("harbor.example.com:5000/app@sha256:" + "b" * 64, "harbor.example.com:5000/app:v2"),
])
def test_replace_image_tag_keeps_registry_port_and_drops_digest(image, expected):
    assert replace_image_tag(image, "v2") == expected


@pytest.mark.parametrize("value,expected", [
    ("500m", 500), ("0.5", 500), ("2", 2000), ("1.25", 1250), (1, 1000), (0.1, 100), ("100u", 1),
    (None, None), ("", None), ("abc", None), (True, None), (-1, None),
])
def test_cpu_quantities_are_millicores(value, expected):
    assert parse_cpu_millicores(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("512Mi", 512), ("1Gi", 1024), ("1.5Gi", 1536), ("536870912", 512), (536870912, 512),
    ("1G", 954), ("1000Ki", 1), ("2048k", 2), (None, None), ("12x", None),
])
def test_memory_quantities_are_mib_rounded_up(value, expected):
    assert parse_memory_mib(value) == expected


def test_replaceable_fields_are_the_flags_the_webhook_can_rewrite():
    args = ["java", "-Xms256m", "-Xmx512m", "-Xss1m", "-XX:+UseG1GC", "-jar", "app.jar", "-XX:MaxMetaspaceSize=64m"]
    # MaxMetaspaceSize after -jar belongs to the application, not the JVM.
    assert replaceable_jvm_fields(args) == {"jvm_xms_bytes", "jvm_xmx_bytes", "jvm_xss_bytes"}
    assert replaceable_jvm_fields(["sh", "-c", "java -Xmx1g -jar app.jar"]) == set()
    replaced = replace_jvm_flags(args, {"jvm_xmx_bytes": 1024 ** 3, "jvm_max_metaspace_bytes": 128 * 1024 ** 2})
    assert replaced[2] == "-Xmx1024m" and replaced[-1] == "-XX:MaxMetaspaceSize=64m"
    assert extract_jvm_config(replaced)["jvm_xmx_bytes"] == 1024 ** 3
