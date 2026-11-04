"""Small stateful Kubernetes CLI double for exercising the real shell installer."""
import base64
import json
import os
from pathlib import Path
import re
import sys

import yaml


OWNER = "setup.kubedoor.io/installation-id"
ID = "718df732-9671-4144-8033-1c531ee41813"
STATE_FILE = Path(os.environ["KUBEDOOR_FAKE_CLUSTER"])
cluster = json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save():
    STATE_FILE.write_text(json.dumps(cluster), encoding="utf-8")


def fail(message):
    print(message, file=sys.stderr)
    save()
    raise SystemExit(1)


def kind_name(value):
    value = value.lower().split(".")[0]
    return {"cm": "configmap", "deploy": "deployment", "sa": "serviceaccount",
            "svc": "service", "ns": "namespace", "po": "pod"}.get(value, value.rstrip("s"))


def key(kind, name):
    return kind_name(kind) + "/" + name


def parts(expression):
    result, current, escaped, brackets = [], "", False, 0
    for char in expression.lstrip("."):
        if escaped:
            current += char
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "." and not brackets:
            result.append(current)
            current = ""
        else:
            brackets += (char == "[") - (char == "]")
            current += char
    if current:
        result.append(current)
    return result


def values_at(obj, expression):
    values = [obj]
    for token in parts(expression):
        match = re.fullmatch(r"([^\[]*)(?:\[(.*)\])?", token)
        if not match:
            fail("Unsupported JSONPath token: " + token)
        field, selector = match.groups()
        found = []
        for value in values:
            if field:
                value = value.get(field) if isinstance(value, dict) else None
            if value is None:
                continue
            if selector is None:
                found.append(value)
            elif selector == "*":
                found.extend(value)
            elif selector.isdigit():
                found.extend(value[int(selector):int(selector) + 1])
            else:
                condition = re.fullmatch(r'''\?\(@\.([\w.]+)\s*==\s*["']([^"']*)["']\)''', selector)
                if not condition:
                    fail("Unsupported JSONPath selector: " + selector)
                name, wanted = condition.groups()
                found.extend(item for item in value if values_at(item, "." + name) == [wanted])
        values = found
    return values


def scalar(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def output(obj, format):
    if format == "json":
        print(json.dumps(obj))
    elif format == "yaml":
        print(yaml.safe_dump(obj))
    elif format == "name":
        print(key(obj["kind"], obj["metadata"]["name"]))
    elif format.startswith("jsonpath="):
        expression = format.partition("=")[2]
        for token in re.findall(r"\{([^{}]*)\}", expression):
            if token.startswith(('"', "'")):
                print(json.loads(token), end="")
            else:
                print(" ".join(scalar(value) for value in values_at(obj, token)), end="")
    elif format.startswith("go-template="):
        template = format.partition("=")[2]
        if 'index .data "SETUP_KEY"' not in template or "base64decode" not in template:
            fail("Unexpected go template: " + template)
        print(base64.b64decode(obj.get("data", {}).get("SETUP_KEY", "")).decode(), end="")
    else:
        fail("Unexpected output format: " + format)


raw = sys.argv[1:]
args, format, namespace = [], "", "kubedoor"
index = 0
while index < len(raw):
    arg = raw[index]
    if arg in ("-n", "--namespace", "--context", "--request-timeout", "-o", "--output"):
        value = raw[index + 1]
        if arg in ("-o", "--output"):
            format = value
        elif arg in ("-n", "--namespace"):
            namespace = value
        index += 2
        continue
    if arg.startswith(("--context=", "--request-timeout=", "--namespace=")):
        index += 1
        continue
    if arg.startswith(("-o=", "--output=")):
        format = arg.partition("=")[2]
    else:
        args.append(arg)
    index += 1

call = {"args": raw, "command": args}
cluster.setdefault("calls", []).append(call)
verb = args[0]
objects = cluster.setdefault("objects", {})

if verb == "get":
    resource = args[1]
    if "/" in resource:
        kind, name = resource.split("/", 1)
    else:
        kind = resource
        name = args[2] if len(args) > 2 and not args[2].startswith("-") else ""
    if kind_name(kind) == "node":
        obj = {"items": [{"status": {"addresses": [
            {"type": "InternalIP", "address": "192.0.2.10"},
        ]}}]}
    else:
        obj = objects.get(key(kind, name))
    if cluster.get("get_error") == key(kind, name):
        fail("simulated API failure")
    if obj:
        output(obj, format or "name")
elif verb in ("apply", "create"):
    content = sys.stdin.read()
    docs = [doc for doc in yaml.safe_load_all(content) if doc]
    docs = [item for doc in docs for item in (doc["items"] if doc.get("kind") == "List" else [doc])]
    call["objects"] = docs
    for obj in docs:
        metadata = obj["metadata"]
        object_key = key(obj["kind"], metadata["name"])
        if verb == "create" and object_key in objects:
            fail("AlreadyExists")
        metadata.setdefault("uid", ID if metadata["name"] == "kubedoor-setup-state" else "uid-" + metadata["name"])
        objects[object_key] = obj
        if obj["kind"] == "Deployment" and metadata["name"] == "kubedoor-setup":
            objects["secret/kubedoor-setup-auth"] = {
                "kind": "Secret", "metadata": {"name": "kubedoor-setup-auth", "annotations": {OWNER: ID}},
                "data": {"SETUP_KEY": base64.b64encode(b"test-install-key").decode()},
            }
        if obj["kind"] == "Service" and metadata["name"] == "kubedoor-web":
            obj["spec"]["ports"][0].setdefault("nodePort", 30080)
elif verb == "annotate":
    kind, name = args[1:3]
    obj = objects[key(kind, name)]
    for arg in args[3:]:
        if "=" in arg and not arg.startswith("-"):
            field, value = arg.split("=", 1)
            obj["metadata"].setdefault("annotations", {})[field] = value
elif verb == "delete":
    if cluster.get("delete_error"):
        fail("Timed out waiting for old job deletion")
    kind, name = args[1:3]
    objects.pop(key(kind, name), None)
    if cluster.get("finish_during_delete"):
        obj = objects["configmap/kubedoor-setup-state"]
        obj["data"]["phase"] = "succeeded"
        obj["data"]["status.json"] = json.dumps({"phase": "succeeded"})
elif verb == "config" and args[1] == "current-context":
    print("test-context")
elif verb == "version":
    print('{"serverVersion":{"gitVersion":"v1.32.0"}}')
elif verb in ("rollout", "wait"):
    pass
else:
    fail("Unexpected kubectl operation: " + " ".join(raw))
save()
