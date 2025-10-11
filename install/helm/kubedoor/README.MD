# 【master端安装】
## 编辑values-master.yaml文件，请仔细阅读注释，根据描述修改配置内容。
## try
helm upgrade -i kubedoor kubedoor-1.3.6.tgz --namespace kubedoor --create-namespace --values values-master.yaml --dry-run --debug
## install
helm upgrade -i kubedoor kubedoor-1.3.6.tgz --namespace kubedoor --create-namespace --values values-master.yaml
## 卸载
helm uninstall kubedoor  -n kubedoor

# 【agent端安装】
## 编辑values-agent.yaml文件，请仔细阅读注释，根据描述修改配置内容。
helm upgrade -i kubedoor-agent kubedoor-1.3.6.tgz --namespace kubedoor --create-namespace --values values-agent.yaml --set tsdb.external_labels_value=dev-k8s

## agent端卸载
helm uninstall kubedoor-agent  -n kubedoor

# 渲染输出模板yaml
helm template . --values values-master.yaml -n kubedoor --create-namespace --output-dir ./kubedoor-master
helm template . --values values-agent.yaml -n kubedoor --create-namespace --output-dir ./kubedoor-agent

# 原文引用
https://github.com/CassInfra/KubeDoor.git