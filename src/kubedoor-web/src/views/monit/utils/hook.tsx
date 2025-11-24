// import "./reset.css";
// import dayjs from "dayjs";
import scale from "../scale/index.vue";
import { message } from "@/utils/message";
import { addDialog } from "@/components/ReDialog";
import { deviceDetection } from "@pureadmin/utils";
import {
  execCapacity,
  execTimeCron,
  rebootResource,
  updateImage
} from "@/api/resource";
import { updatePodCount, showAddLabel } from "@/api/monit";
import { h, ref } from "vue";
import { transformI18n } from "@/plugins/i18n";

export function useResource() {
  const ScaleRef = ref();

  function onChangeCapacity(row: any) {
    return new Promise(async resolve => {
      let currentEnv = "";
      const rowArr = [row];
      const params = [];
      const content = rowArr
        .map(row => {
          let podCount = row.podCount;
          const item = {
            namespace: row.namespace,
            deployment_name: row.deployment,
            num: Number(podCount)
          };
          params.push(item);
          currentEnv = row.env;
          return (
            `<div>` +
            `<strong style='margin-right: 5px;'>Env: </strong><i><span style='color: red;'>${row.env}</span></i>&nbsp;&nbsp;` +
            `<strong style='margin-right: 5px;'>Namespace: </strong><i><span style='color: red;'>${row.namespace}</span></i>&nbsp;&nbsp;` +
            `<strong style='margin-right: 5px;'>Deployment: </strong><i><span style='color: red;'>${row.deployment}</span></i>&nbsp;&nbsp;` +
            // `<strong style='margin-right: 5px;'>Pod数量: </strong><i><span style='color: red;'>${podCount}</span></i>` +
            `</div>`
          );
        })
        .join("<br>");

      const showAddLabelRes = await showAddLabel(row.env, row.namespace);

      const scaleProps = {
        isScale: true,
        content,
        showInterval: params.length > 1, // 是否显示间隔
        showAddLabel: showAddLabelRes.data.length > 0,
        params: {
          podCount: row.podCount,
          env: row.env,
          namespace: row.namespace,
          deployment: row.deployment
        }
      };

      addDialog({
        title: transformI18n("resource.scale"),
        props: scaleProps,
        width: "40%",
        draggable: true,
        fullscreen: deviceDetection(),
        closeOnClickModal: false,
        sureBtnLoading: true,
        contentRenderer: () => h(scale, { ref: ScaleRef, ...scaleProps }),
        beforeSure: async done => {
          const scaleData = await ScaleRef.value.getData();
          // 更新扩缩容量pod数 - 如果勾选了temp则跳过SQL更新
          if (!scaleData.temp) {
            await updatePodCount({
              env: row.env,
              namespace: row.namespace,
              deployment_name: row.deployment,
              pod_count_manual: scaleData.podCount
            });
          }
          let res;
          params.map(item => {
            item.num = scaleData.podCount;
            return item;
          });

          // 构建新的数据格式
          const requestData = {
            deployment_list: params,
            node_scheduler: scaleData.selectedNodes || []
          };

          if (scaleData.tempData) {
            if (params.length > 1 || scaleData.tempData.type == 1) {
              res = await execCapacity(
                currentEnv,
                scaleData.tempData.add_label,
                requestData,
                params.length > 1 ? scaleData.tempData.interval : undefined,
                scaleData.temp,
                scaleData.strategy,
                scaleData.scheduler
              );
            } else {
              let tempData = {
                type: "scale",
                service: requestData,
                time: "",
                cron: ""
              };
              if (scaleData.tempData.type == 2) {
                tempData.time = scaleData.tempData.time;
                tempData.cron = "";
              } else if (scaleData.tempData.type == 3) {
                tempData.time = "";
                tempData.cron = scaleData.tempData.cron;
              }
              res = await execTimeCron(
                currentEnv,
                scaleData.tempData.add_label,
                tempData,
                scaleData.temp,
                scaleData.strategy,
                scaleData.scheduler
              );
            }

            if ((res as any).message == "ok" || res == "ok") {
              message(transformI18n("resource.message.editSuccess"), {
                type: "success"
              });
              done();
              resolve(true);
            } else {
              message((res as any).message, {
                type: "error"
              });
            }
          }
        },
        closeCallBack: () => {
          resolve(true);
        }
      });
    });
  }

  function onReboot(row: any) {
    return new Promise(resove => {
      let currentEnv = "";
      const rowArr = [row];
      let params = [];
      const content = rowArr
        .map(row => {
          params.push({
            namespace: row.namespace,
            deployment_name: row.deployment
          });
          currentEnv = row.env;
          return (
            `<div>` +
            `<strong style='margin-right: 5px;'>Env: </strong><i><span style='color: red;'>${row.env}</span></i>&nbsp;&nbsp;` +
            `<strong style='margin-right: 5px;'>Namespace: </strong><i><span style='color: red;'>${row.namespace}</span></i>&nbsp;&nbsp;` +
            `<strong style='margin-right: 5px;'>Deployment: </strong><i><span style='color: red;'>${row.deployment}</span></i>&nbsp;&nbsp;` +
            `</div>`
          );
        })
        .join("<br>");

      const rebootScaleProps = {
        isScale: false,
        content,
        showInterval: params.length > 1,
        showAddLabel: false,
        params: {
          env: row.env,
          namespace: row.namespace,
          deployment: row.deployment
        }
      };

      addDialog({
        title: transformI18n("resource.reboot"),
        width: "40%",
        props: rebootScaleProps,
        draggable: true,
        fullscreen: deviceDetection(),
        closeOnClickModal: false,
        contentRenderer: () => h(scale, { ref: ScaleRef, ...rebootScaleProps }),
        beforeSure: async done => {
          const scaleData = await ScaleRef.value.getData();
          let res;
          params.map(item => {
            return item;
          });
          const requestData = params;
          if (scaleData.tempData) {
            if (params.length > 1 || scaleData.tempData.type == 1) {
              res = await rebootResource(
                currentEnv,
                requestData,
                params.length > 1 ? scaleData.tempData.interval : undefined,
                scaleData.scheduler,
                scaleData.selectedNodes
              );
            } else {
              let tempData = {
                type: "restart",
                service: requestData,
                time: "",
                cron: ""
              };
              if (scaleData.tempData.type == 2) {
                tempData.time = scaleData.tempData.time;
                tempData.cron = "";
              } else if (scaleData.tempData.type == 3) {
                tempData.time = "";
                tempData.cron = scaleData.tempData.cron;
              }
              res = await execTimeCron(
                currentEnv,
                false,
                tempData,
                false,
                undefined,
                scaleData.scheduler
              );
            }

            console.log(res);

            if (
              res &&
              (res as any).error_list &&
              (res as any).error_list.length > 0
            ) {
              let errorMsg = "";
              (res as any).error_list.forEach(err => {
                errorMsg += `Namespace: ${err.namespace}, Deployment: ${err.deployment_name}, Reason: ${err.reason}<br/>`;
              });
              message(errorMsg, {
                type: "error",
                dangerouslyUseHTMLString: true
              });
            } else if ((res as any).message == "ok" || res == "ok") {
              message(transformI18n("resource.message.editSuccess"), {
                type: "success"
              });
              done();
              resove(true);
            } else {
              message((res as any).message, {
                type: "error"
              });
            }
          }
        },
        closeCallBack: () => {
          resove(true);
        }
      });
    });
  }

  function onUpdateImage(env: string, data: any) {
    return new Promise(async resolve => {
      const res = await updateImage(env, data);
      if ((res as any).success) {
        message(transformI18n((res as any).message), {
          type: "success"
        });
        resolve(true);
      } else {
        message((res as any).error || (res as any).message, {
          type: "error"
        });
      }
    });
  }

  return {
    onChangeCapacity,
    onReboot,
    onUpdateImage
  };
}
