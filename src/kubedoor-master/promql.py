query_dict = {
    # 工作负载列表与pod数
    "pod_num": '''
min_over_time(
  label_replace(
    count by ({env_key} namespace, owner_name) (
        (
            kube_pod_status_phase{{env} phase="Running"} == 1
          and on ({env_key} namespace,pod)
            kube_pod_status_ready{{env} condition="true"} == 1
        )
      * on ({env_key} namespace,pod) group_left (owner_name)
        kube_pod_owner{{env} owner_kind="ReplicaSet", owner_is_controller="true"}
    ),
    "workload",
    "$1",
    "owner_name",
    "^(.*)-[a-z0-9]+$"
  )[{duration}:]
)
''',
    # 使用核数P95
    "core_usage": '''
quantile_over_time(
  0.80,
  avg by ({env_key} namespace, owner_name) (
      irate(
        container_cpu_usage_seconds_total{{env} container!="",container!="POD"}[3m]
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)
''',
    # CPU使用率P95
    "core_usage_percent": '''
quantile_over_time(
  0.80,
  avg by ({env_key} namespace, owner_name) (
      max by ({env_key} namespace, pod) (
        irate(
          container_cpu_usage_seconds_total{{env} container!="",container!="POD"}[3m]
        )
      )
    /
      max by ({env_key} namespace, pod) (
        container_spec_cpu_quota{{env} container!="",container!="POD"}
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)*10000000
''',
    # WSS内存使用MB P95
    "wss_usage_MB": '''
quantile_over_time(
  0.80,
  avg by ({env_key} namespace, owner_name) (
      container_memory_working_set_bytes{{env} container!="",container!="POD"}
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)/1024/1024
''',
    # WSS内存使用率P95
    "wss_usage_percent": '''
quantile_over_time(
  0.80,
  avg by ({env_key} namespace, owner_name) (
      max by ({env_key} namespace, pod) (
        container_memory_working_set_bytes{{env} container!="",container!="POD"}
      )
    /
      max by ({env_key} namespace, pod) (
        kube_pod_container_resource_limits{{env} container!="",container!="POD",resource="memory",unit="byte"}
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)*100
''',
    # CPU limit
    "limit_core": '''
max_over_time(
  max by ({env_key} namespace, owner_name) (
      max by ({env_key} namespace, pod) (
        kube_pod_container_resource_limits{{env}container!="",container!="POD",resource="cpu",unit="core"}
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)*1000
''',
    # 内存limit_MB
    "limit_mem_MB": '''
max_over_time(
  max by ({env_key} namespace, owner_name) (
      max by ({env_key} namespace, pod) (
        kube_pod_container_resource_limits{{env}container!="",container!="POD",resource="memory",unit="byte"}
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)/1024/1024
''',
    # CPU request
    "request_core": '''
max_over_time(
  max by ({env_key} namespace, owner_name) (
      max by ({env_key} namespace, pod) (
        kube_pod_container_resource_requests{{env}container!="",container!="POD",resource="cpu",unit="core"}
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)*1000
''',
    # 内存request_MB
    "request_mem_MB": '''
max_over_time(
  max by ({env_key} namespace, owner_name) (
      max by ({env_key} namespace, pod) (
        kube_pod_container_resource_requests{{env}container!="",container!="POD",resource="memory",unit="byte"}
      )
    * on ({env_key} namespace,pod) group_left (owner_name)
      kube_pod_owner{{env} owner_is_controller="true",owner_kind="ReplicaSet"}
  )[{duration}:]
)/1024/1024
''',
    # 查询节点的所有deployment列表
    "deployments_by_node": 'kube_pod_info{{env}created_by_kind="ReplicaSet", namespace!~"{namespace}", node="{node}"}',
}


node_rank_query = {
    # 节点pod数
    "pod": '''
count by (node) (kube_pod_info{{env} created_by_kind!~"<none>|Job"})
''',
    # 节点实时CPU使用率
    "cpu": '''
sum by (instance) (
  irate(
    container_cpu_usage_seconds_total{{env} container!="",container!="POD"}[3m]
  )
)
/
sum by (instance) (
  label_replace(
    kube_node_status_allocatable{{env} resource="cpu",unit="core"},
    "instance",
    "$1",
    "node",
    "(.*)"
  )
) * 100
''',
    # 节点实时内存使用率
    "mem": '''
sum by (instance) (
  container_memory_working_set_bytes{{env} container!="",container!="POD"}
)
/
sum by (instance) (
  label_replace(
    kube_node_status_allocatable{{env} resource="memory",unit="byte"},
    "instance",
    "$1",
    "node",
    "(.*)"
  )
) * 100
''',
    # 高峰期CPU负载
    "peak_cpu": '''
sum by (node) (
  kube_pod_container_resource_requests{{env} container!="",container!="POD",resource="cpu",unit="core"}
)
''',
    # 高峰期内存
    "peak_mem": '''
sum by (node) (
  kube_pod_container_resource_requests{{env} container!="",container!="POD",resource="memory",unit="byte"}
)/1024/1024/1024
''',
}

# 获取deployment的image列表

deployment_image = {
    "promql": '''
group by ({env_key} namespace, owner_name, image_spec, image) (
    label_replace(
        kube_pod_container_info{{env} namespace="{namespace}", container_id!=""}
      * on ({env_key} namespace, pod) group_left (owner_name)
        kube_pod_owner{{env} namespace="{namespace}",owner_kind="ReplicaSet",owner_is_controller="true"},
      "replicaset",
      "$1",
      "owner_name",
      "(.*)"
    )
  * on ({env_key} namespace, replicaset) group_left (owner_name)
    kube_replicaset_owner{{env} namespace="{namespace}",owner_kind="Deployment",owner_name="{deployment}"}
)
'''
}

deployment_image_min = {
    "promql": '''
group by ({env_key} namespace, owner_name, image_spec, image) (
    label_replace(
        bottomk(1,count_over_time(kube_pod_container_info{{env} namespace="{namespace}", container_id!=""}[10m]))
      * on ({env_key} namespace, pod) group_left (owner_name)
        kube_pod_owner{{env} namespace="{namespace}",owner_kind="ReplicaSet",owner_is_controller="true"},
      "replicaset",
      "$1",
      "owner_name",
      "(.*)"
    )
  * on ({env_key} namespace, replicaset) group_left (owner_name)
    kube_replicaset_owner{{env} namespace="{namespace}",owner_kind="Deployment",owner_name="{deployment}"}
)
'''
}


# 获取deployment的pod所在的节点分布

deployment_node = {
    "promql": '''
count by (node) (
    kube_pod_info{{env} created_by_kind="ReplicaSet",namespace="{namespace}",created_by_name=~"{deployment}.*"}
  and on ({env_key} namespace, created_by_name)
    label_replace(
      kube_replicaset_owner{{env} owner_kind="Deployment",namespace="{namespace}",owner_name="{deployment}"},
      "created_by_name",
      "$1",
      "replicaset",
      "(.*)"
    )
)
'''
}
