import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio import client
from kubernetes_asyncio.client.rest import ApiException


@pytest.mark.parametrize("size,expected", [
    ("1024m", 1024 ** 3), ("1G", 1024 ** 3), ("512K", 512 * 1024),
    ("204800k", 200 * 1024 ** 2), ("17", 17), ("0", 0), ("0m", 0),
    (str((1 << 63) - 1), (1 << 63) - 1), (str(1 << 63), None),
    ("8589934592g", None), ("1.5m", None), ("-1m", None), ("1mb", None),
    ("1 m", None), ("", None), ("1m\n", None),
])
def test_parse_exact_bytes_and_reject_invalid_sizes(jvm_module, size, expected):
    assert jvm_module.parse_jvm_size(size) == expected


def test_extract_duplicates_and_keep_zero(jvm_module):
    args = ["java", "-Xms0", "-Xmx256m", "-Xss512k", "-XX:MaxMetaspaceSize=200M", "-Xmx1g", "-jar", "app.jar"]
    assert jvm_module.extract_jvm_config(args) == {
        "jvm_xms_bytes": 0, "jvm_xmx_bytes": 1024 ** 3,
        "jvm_xss_bytes": 512 * 1024, "jvm_max_metaspace_bytes": 200 * 1024 ** 2,
    }


@pytest.mark.parametrize("invalid", ["-Xmx1.5m", "-Xmx8589934592g"])
def test_invalid_final_duplicate_does_not_keep_prior_value(jvm_module, invalid):
    assert jvm_module.extract_jvm_config(["java", "-Xmx256m", invalid])["jvm_xmx_bytes"] is None


@pytest.mark.parametrize("args", [None, [], ["/usr/bin/java", "-Xmx1g"], ["sh", "-c", "java -Xmx1g"], ["JAVA", "-Xmx1g"]])
def test_strict_java_args_gate(jvm_module, args):
    assert all(value is None for value in jvm_module.extract_jvm_config(args).values())
    assert jvm_module.replace_jvm_flags(args, {"jvm_xmx_bytes": 2 * 1024 ** 3}) == args


@pytest.mark.parametrize("application", [["-jar", "app.jar"], ["Main"], ["-m", "app/main"], ["--module=app/main"]])
def test_never_change_application_arguments(jvm_module, application):
    args = ["java", "-Xms1g", *application, "-Xmx123m", "-XX:MaxMetaspaceSize=55m"]
    assert jvm_module.extract_jvm_config(args)["jvm_xmx_bytes"] is None
    replaced = jvm_module.replace_jvm_flags(args, {"jvm_xms_bytes": 512 * 1024 ** 2, "jvm_xmx_bytes": 1024 ** 3})
    assert replaced == ["java", "-Xms512m", *application, "-Xmx123m", "-XX:MaxMetaspaceSize=55m"]


def test_classpath_operand_is_not_a_jvm_option(jvm_module):
    args = ["java", "-cp", "-Xmx128m", "-Xmx256m", "Main", "-Xmx512m"]
    assert jvm_module.extract_jvm_config(args)["jvm_xmx_bytes"] == 256 * 1024 ** 2
    assert jvm_module.replace_jvm_flags(args, {"jvm_xmx_bytes": 1024 ** 3}) == ["java", "-cp", "-Xmx128m", "-Xmx1024m", "Main", "-Xmx512m"]


def test_replace_by_name_preserves_order_and_unmanaged_fields(jvm_module):
    args = ["java", "-Xss512k", "-Xmx256m", "-XX:MaxMetaspaceSize=200m", "-XX:MetaspaceSize=50m", "-Xmx512m", "-Dfoo=bar", "-XX:OnOutOfMemoryError=/a.sh /dump.hprof", "-jar", "app.jar"]
    original = list(args)
    config = {"jvm_xmx_bytes": 1024 ** 3, "jvm_xss_bytes": 524288, "jvm_xms_bytes": 1024 ** 3, "jvm_max_metaspace_bytes": None}
    replaced = jvm_module.replace_jvm_flags(args, config)
    assert replaced == ["java", "-Xss512k", "-Xmx1024m", "-XX:MaxMetaspaceSize=200m", "-XX:MetaspaceSize=50m", "-Xmx1024m", "-Dfoo=bar", "-XX:OnOutOfMemoryError=/a.sh /dump.hprof", "-jar", "app.jar"]
    assert args == original
    assert not any(item.startswith("-Xms") for item in replaced)


@pytest.mark.parametrize("size,formatted", [(512 * 1024, "512k"), (512 * 1024 ** 2, "512m"), (12345, "12345"), (0, "0m")])
def test_format_uses_valid_integral_units(jvm_module, size, formatted):
    assert jvm_module.format_jvm_size(size) == formatted
    assert jvm_module.parse_jvm_size(formatted) == size


@pytest.mark.parametrize("size,unit,formatted", [
    (1024 * 1024, "k", "1024k"),
    (512 * 1024, "k", "512k"),
    (0, "k", "0k"),
    (1024 ** 3, "m", "1024m"),
    (0, "m", "0m"),
    (12345, "k", "12345"),
])
def test_field_specific_units_preserve_size(jvm_module, size, unit, formatted):
    assert jvm_module.format_jvm_size(size, unit) == formatted
    assert jvm_module.parse_jvm_size(formatted) == size


def test_replacement_uses_k_for_stack_even_when_whole_megabytes(jvm_module):
    args = ["java", "-Xms1g", "-Xmx2g", "-Xss1m", "-XX:MaxMetaspaceSize=204800k", "-jar", "app.jar"]
    config = jvm_module.extract_jvm_config(args)
    assert jvm_module.replace_jvm_flags(args, config) == [
        "java", "-Xms1024m", "-Xmx2048m", "-Xss1024k", "-XX:MaxMetaspaceSize=200m", "-jar", "app.jar",
    ]


@pytest.mark.parametrize("invalid", [None, -1, True, 1.5, "1024", 1 << 63])
def test_invalid_control_value_is_not_applied(jvm_module, invalid):
    args = ["java", "-Xmx256m"]
    assert jvm_module.replace_jvm_flags(args, {"jvm_xmx_bytes": invalid}) == args


def deployment(name, args, sidecar_args=None):
    containers = [client.V1Container(name="main", args=args)]
    if sidecar_args:
        containers.append(client.V1Container(name="sidecar", args=sidecar_args))
    return client.V1Deployment(
        metadata=client.V1ObjectMeta(name=name, namespace="team"),
        spec=client.V1DeploymentSpec(
            selector=client.V1LabelSelector(match_labels={"app": name}),
            template=client.V1PodTemplateSpec(spec=client.V1PodSpec(containers=containers)),
        ),
    )


@pytest.mark.asyncio
async def test_list_paginates_and_only_uses_first_container(jvm_module):
    pages = [
        client.V1DeploymentList(metadata=client.V1ListMeta(_continue="page-2"), items=[
            deployment("valid", ["java", "-Xmx1g", "-jar", "app.jar"]),
            deployment("sidecar-only", ["nginx"], ["java", "-Xmx1g"]),
        ]),
        client.V1DeploymentList(metadata=client.V1ListMeta(), items=[
            deployment("zero", ["java", "-Xms0"]),
            deployment("invalid", ["java", "-Xmx1.5g"]),
        ]),
    ]
    api = SimpleNamespace(list_deployment_for_all_namespaces=AsyncMock(side_effect=pages), read_namespaced_deployment=AsyncMock())
    response = await jvm_module.get_jvm_configs(api, SimpleNamespace(query={}))
    payload = json.loads(response.body)
    assert payload["success"] is True
    assert [row["deployment"] for row in payload["data"]] == ["valid", "zero"]
    assert payload["data"][0]["jvm_xmx_bytes"] == 1024 ** 3
    assert payload["data"][1]["jvm_xms_bytes"] == 0
    assert api.list_deployment_for_all_namespaces.await_args_list[0].kwargs["limit"] == 500
    assert [call.kwargs["_continue"] for call in api.list_deployment_for_all_namespaces.await_args_list] == [None, "page-2"]
    api.read_namespaced_deployment.assert_not_called()


@pytest.mark.asyncio
async def test_namespace_list_uses_namespace_endpoint(jvm_module):
    api = SimpleNamespace(list_namespaced_deployment=AsyncMock(return_value=client.V1DeploymentList(metadata=client.V1ListMeta(), items=[])))
    response = await jvm_module.get_jvm_configs(api, SimpleNamespace(query={"namespace": "team"}))
    assert json.loads(response.body) == {"success": True, "data": []}
    assert api.list_namespaced_deployment.await_args.kwargs["namespace"] == "team"


@pytest.mark.asyncio
async def test_failed_later_page_does_not_return_partial_snapshot(jvm_module):
    first_page = client.V1DeploymentList(metadata=client.V1ListMeta(_continue="page-2"), items=[deployment("valid", ["java", "-Xmx1g"])])
    api = SimpleNamespace(list_deployment_for_all_namespaces=AsyncMock(side_effect=[first_page, ApiException(status=410, reason="Expired")]))
    response = await jvm_module.get_jvm_configs(api, SimpleNamespace(query={}))
    payload = json.loads(response.body)
    assert response.status == 502
    assert payload["success"] is False
    assert "data" not in payload
