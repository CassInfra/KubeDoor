<template>
  <div class="system-permissions">
    <el-alert type="info" :closable="false" class="mb-3">
      <template #title>
        <ul class="intro">
          <li>
            每一行是一种操作,勾选的列表示该集群权限的账号可以执行。集群权限在「用户与权限」里按集群设置,系统管理员在所有集群都是「管理员」;没有列出的查询类操作,能看到集群就能做。
          </li>
          <li>
            三列保持「只读 ⊆ 读写 ⊆
            管理员」,勾选时自动联动;取消「管理员」列等于关闭这个操作,所有人(包括系统管理员)都不能执行。
          </li>
          <li>
            与具体集群无关的配置(没限定集群的告警屏蔽、节点均衡的全局配置)按全局角色查表:系统管理员、集群管理员看「管理员」列,读写看「读写」列,只读看「只读」列。
          </li>
          <li>
            网页、企微 AI、外部 MCP
            都按这张表判断:没有权限直接拒绝,有权限再看「审批」列要不要提问人确认。保存后立即生效。
          </li>
        </ul>
      </template>
    </el-alert>

    <el-card v-loading="loading" class="mb-3">
      <template #header>
        <div class="card-header">
          <div>
            <span class="card-title">操作权限与审批</span>
            <span v-if="data" class="sub-text ml-2">{{ updatedText }}</span>
          </div>
          <div>
            <el-button :disabled="!data || isDefault" @click="restoreDefaults">
              恢复默认
            </el-button>
            <el-button
              type="primary"
              :disabled="!dirty"
              :loading="saving"
              @click="save"
            >
              保存
            </el-button>
          </div>
        </div>
      </template>

      <el-table
        :data="rows"
        row-key="key"
        border
        :span-method="spanMethod"
        class="policy-table"
      >
        <el-table-column label="分组" prop="group" width="110" align="center" />
        <el-table-column label="操作" min-width="320">
          <template #default="{ row }">
            <div class="op-name">
              <span :class="{ 'is-highlight': row.highlight }">
                {{ row.name }}
              </span>
              <el-tooltip
                v-if="OP_HINTS[row.key]"
                :content="OP_HINTS[row.key]"
                placement="top"
                popper-class="permission-hint"
              >
                <el-icon class="hint-icon"><QuestionFilled /></el-icon>
              </el-tooltip>
            </div>
            <div v-if="row.desc" class="sub-text">{{ row.desc }}</div>
          </template>
        </el-table-column>
        <el-table-column
          label="适用"
          prop="applies"
          width="90"
          align="center"
        />
        <el-table-column
          v-for="level in PERMISSION_LEVELS"
          :key="level"
          :label="PERM_LABELS[level]"
          width="80"
          align="center"
        >
          <template #default="{ row }">
            <el-checkbox
              v-if="form.ops[row.key]"
              :model-value="form.ops[row.key][level]"
              @update:model-value="value => onLevelChange(row, level, !!value)"
            />
          </template>
        </el-table-column>
        <el-table-column width="90" align="center">
          <template #header>
            <span>审批</span>
            <el-tooltip
              :content="APPROVAL_HINT"
              placement="top"
              popper-class="permission-hint"
            >
              <el-icon class="hint-icon"><QuestionFilled /></el-icon>
            </el-tooltip>
          </template>
          <template #default="{ row }">
            <span v-if="approvalNA(row)" class="sub-text">—</span>
            <el-tooltip
              v-else-if="row.approval_fixed"
              content="高危命令固定需要审批,不能取消"
              placement="top"
            >
              <el-checkbox :model-value="true" disabled />
            </el-tooltip>
            <el-checkbox
              v-else-if="form.ops[row.key]"
              v-model="form.ops[row.key].approval"
            />
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-row :gutter="12">
      <el-col :xs="24" :lg="14">
        <el-card v-loading="loading" class="mb-3">
          <template #header>
            <div class="card-header">
              <span class="card-title">
                查看命令({{ form.commands.view.length }})
              </span>
              <div class="add-command">
                <el-input
                  v-model="newCommand.view"
                  placeholder="命令名,如 ls"
                  :maxlength="COMMAND_MAX_LENGTH"
                  clearable
                  @keyup.enter="addCommand('view')"
                />
                <el-button type="primary" plain @click="addCommand('view')">
                  添加
                </el-button>
              </div>
            </div>
          </template>
          <el-alert type="info" :closable="false" class="mb-2">
            <template #title>
              AI 在 Pod 内执行(pod_exec、kubectl
              exec)的命令里,每个命令都在这个清单里、并且通过内置参数检查,才算查看命令;网页终端不受清单影响。参数检查只决定归哪一类,不是禁止:不满足的继续按高危
              / 其他命令判断,例如 curl -X POST 归其他命令,find -delete
              归高危命令。所有查看命令都不能读
              /var/run/secrets、/run/secrets,输出重定向只能到
              /dev/null。自己添加的命令没有参数检查,不要加 awk、sed、python
              这类能执行任意命令的程序。
            </template>
          </el-alert>
          <el-table :data="viewRows" max-height="420" size="small" border>
            <el-table-column label="命令" prop="command" width="130" />
            <el-table-column label="内置参数检查" min-width="260">
              <template #default="{ row }">
                <span v-if="row.check">{{ row.check }}</span>
                <span v-else class="sub-text">—</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="70" align="center">
              <template #default="{ row }">
                <el-button
                  link
                  type="danger"
                  @click="removeCommand('view', row.command)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <el-col :xs="24" :lg="10">
        <el-card v-loading="loading" class="mb-3">
          <template #header>
            <div class="card-header">
              <span class="card-title">
                高危命令({{ form.commands.danger.length }})
              </span>
              <div class="add-command">
                <el-input
                  v-model="newCommand.danger"
                  placeholder="rm、mkfs* 或 find -delete"
                  :maxlength="COMMAND_MAX_LENGTH"
                  clearable
                  @keyup.enter="addCommand('danger')"
                />
                <el-button type="primary" plain @click="addCommand('danger')">
                  添加
                </el-button>
              </div>
            </div>
          </template>
          <el-alert type="warning" :closable="false" class="mb-2">
            <template #title>
              不是查看命令、并且命中这个清单的,算高危命令,固定需要审批。宽匹配、宁可误报:命令里任何位置出现都算(包括
              sh -c 里组合的、带路径的)。可以写「命令」、「前缀*」(如 mkfs* 匹配
              mkfs.ext4)或「命令 参数」(如 find -delete)。
            </template>
          </el-alert>
          <el-table :data="dangerRows" max-height="420" size="small" border>
            <el-table-column label="命令" prop="command" width="130" />
            <el-table-column label="匹配方式" prop="match" min-width="160" />
            <el-table-column label="操作" width="70" align="center">
              <template #default="{ row }">
                <el-button
                  link
                  type="danger"
                  @click="removeCommand('danger', row.command)"
                >
                  删除
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessageBox } from "element-plus";
import { QuestionFilled } from "@element-plus/icons-vue";
import dayjs from "dayjs";
import { message } from "@/utils/message";
import { useUserStoreHook } from "@/store/modules/user";
import {
  type PermissionOperation,
  type PermissionPolicyData,
  getPermissionPolicy,
  savePermissionPolicy
} from "@/api/system";
import {
  COMMAND_MAX_LENGTH,
  PERMISSION_LEVELS,
  PERM_LABELS,
  VIEW_COMMAND_CHECKS,
  type CommandKind,
  type PermissionLevel,
  type PolicyValue,
  commandError,
  dangerMatchText,
  linkLevels,
  normalizeCommand,
  samePolicy
} from "@/utils/permission";

defineOptions({ name: "SystemPermissions" });

/** 操作列后面问号里的说明(只给容易误解的行) */
const OP_HINTS: Record<string, string> = {
  "pod.shell":
    "网页终端是完整的交互式 shell,没法按命令限制,这一行只管能不能打开终端。AI 在 Pod 内执行命令是下面三行,按命令清单分成查看 / 其他 / 高危分别控制。",
  "node.shell":
    "在节点上起一个特权 Pod,以 root 进入宿主机,同样没法按命令限制。",
  "pod.exec.view":
    "AI 用 pod_exec 或 kubectl exec 执行的命令里,每个命令都在「查看命令」清单里、并且通过内置参数检查的,算这一类。",
  "pod.exec.other":
    "既不是查看命令、也没命中「高危命令」清单的,算这一类,例如 touch、curl -X POST。",
  "pod.exec.danger": "命中「高危命令」清单的算这一类,审批固定需要,不能取消。",
  "workload.image":
    "网页上改镜像时,没有这一行权限的只读账号,还可以按 UPDATE_IMAGE 配置(集群、时间窗口、用户白名单)放行;AI 改镜像不走这个白名单。",
  "resource.secret":
    "查看 Secret 的内容(网页的资源查看、AI 读取 Secret)。只列出 Secret、不看内容的不受这一行限制。",
  "ai.k8s_write":
    "AI 用 kubectl、istioctl、K8S API 做修改要这一行的权限,还要叠加具体操作那一行:例如 kubectl delete pod 还要「删除 Pod」,kubectl scale 还要「扩缩容」,改 CoreDNS / Istio 资源还要对应的网络行;认不出具体操作的只看这一行。能改的范围很宽,所以标红。",
  "ai.diagnostic":
    "诊断工具不进业务 Pod,是在 KubeDoor 自己的容器(agent 或 AI 服务)里执行的,例如从集群内 curl 某个服务、nc -z 测端口、nmap 扫描、tcpdump 抓包、iperf3 测带宽。这些工具不分 GET 还是 POST,都按这一行判断。jq、dig、traceroute、ps、vmstat 这类只读的诊断工具算查询,不受这一行限制。AI 进到业务 Pod 里执行的 curl 不是这一行,按上面「AI 在 Pod 内执行」三行判断。",
  "balance.run":
    "分析、执行、清理按所选集群的权限判断;全局配置和具体集群无关,按全局角色查这一行。",
  "network.coredns":
    "CoreDNS 页面的保存,以及通过编辑 YAML、删除资源、AI 改 kube-system/coredns,都按这一行判断,不能绕过。",
  "network.istio":
    "全局VS管理的修改,以及通过编辑 YAML、AI 改 Istio 资源(VirtualService、DestinationRule、Gateway 等),都按这一行判断,不能绕过。全局VS管理里,改组、改组标准这类组级修改要求组内每个集群都有这个权限;下发、回退、删除集群里的 VirtualService、刷新现状只看目标集群。",
  "alert.silence":
    "限定了集群的屏蔽规则按该集群的权限判断;没限定集群的规则和具体集群无关,按全局角色查这一行。"
};

const APPROVAL_HINT =
  "AI 执行这一类操作前,是否要提问人自己确认后才执行(网页、企微、外部 MCP 一样;网页勾了「自动批准执行」时自动批准)。「—」表示不是 AI 操作,不需要审批;AI 的查询永远不需要审批;高危命令固定需要审批。";

const loading = ref(false);
const saving = ref(false);
const data = ref<PermissionPolicyData | null>(null);
const form = ref<PolicyValue>({ ops: {}, commands: { view: [], danger: [] } });
const newCommand = reactive<Record<CommandKind, string>>({
  view: "",
  danger: ""
});

const clone = (value: PolicyValue): PolicyValue =>
  JSON.parse(JSON.stringify(value));

/** 同组的行排在一起,组的顺序按第一次出现 */
const rows = computed(() => {
  const groups = new Map<string, PermissionOperation[]>();
  for (const item of data.value?.definitions ?? []) {
    if (!groups.has(item.group)) groups.set(item.group, []);
    groups.get(item.group)!.push(item);
  }
  return [...groups.values()].flat();
});

/** 审批列显示「—」:不是 AI 操作(审批固定为不需要) */
const approvalNA = (row: PermissionOperation) =>
  row.approval_na ?? (row.approval_fixed && !row.approval);

/** 分组列合并同组的单元格 */
function spanMethod({
  row,
  rowIndex,
  columnIndex
}: {
  row: PermissionOperation;
  rowIndex: number;
  columnIndex: number;
}) {
  if (columnIndex !== 0) return;
  const list = rows.value;
  if (rowIndex > 0 && list[rowIndex - 1].group === row.group) {
    return { rowspan: 0, colspan: 0 };
  }
  let span = 1;
  while (list[rowIndex + span]?.group === row.group) span++;
  return { rowspan: span, colspan: 1 };
}

const dirty = computed(
  () => !!data.value && !samePolicy(form.value, data.value.policy)
);
const isDefault = computed(
  () => !!data.value && samePolicy(form.value, data.value.defaults)
);
const updatedText = computed(() => {
  const value = data.value;
  if (!value?.updated_at) return "还没修改过,当前是出厂默认值";
  const at = dayjs(value.updated_at).format("YYYY-MM-DD HH:mm:ss");
  return `最后修改:${value.updated_by || "-"} · ${at}`;
});

/** 查看命令的内置参数检查说明:以后端返回的为准,没有的(如 ncat、netcat 这类别名)用前端的 */
const builtinChecks = computed<Record<string, string>>(() => ({
  ...VIEW_COMMAND_CHECKS,
  ...(data.value?.builtin_checks ?? {})
}));
const viewRows = computed(() =>
  form.value.commands.view.map(command => ({
    command,
    check: builtinChecks.value[command] ?? ""
  }))
);
const dangerRows = computed(() =>
  form.value.commands.danger.map(command => ({
    command,
    match: dangerMatchText(command)
  }))
);

function apply(value: PermissionPolicyData) {
  data.value = value;
  form.value = clone(value.policy);
}

async function load() {
  loading.value = true;
  try {
    const res = await getPermissionPolicy();
    if (res?.success) apply(res.data);
  } catch {
    // 错误提示由 http 统一弹出
  } finally {
    loading.value = false;
  }
}

/** 勾选联动;取消「管理员」列等于全站关闭这个操作,先确认 */
async function onLevelChange(
  row: PermissionOperation,
  level: PermissionLevel,
  checked: boolean
) {
  const op = form.value.ops[row.key];
  if (!op) return;
  const before = { read: op.read, rw: op.rw, admin: op.admin };
  // 先改,界面和数据保持一致;不确认时再改回来
  Object.assign(op, linkLevels(before, level, checked));
  if (level !== "admin" || checked) return;
  try {
    await ElMessageBox.confirm(
      `取消「管理员」列后,所有人(包括系统管理员)都不能执行「${row.name}」,相当于全站关闭这个操作。确定吗?`,
      "关闭操作",
      {
        type: "warning",
        confirmButtonText: "确定关闭",
        cancelButtonText: "保留"
      }
    );
  } catch {
    Object.assign(op, before);
  }
}

function addCommand(kind: CommandKind) {
  const value = normalizeCommand(newCommand[kind]);
  const error = commandError(kind, value, form.value.commands);
  if (error) {
    message(error, { type: "warning" });
    return;
  }
  form.value.commands[kind].push(value);
  newCommand[kind] = "";
}

function removeCommand(kind: CommandKind, command: string) {
  const list = form.value.commands[kind];
  const index = list.indexOf(command);
  if (index >= 0) list.splice(index, 1);
}

async function restoreDefaults() {
  if (!data.value) return;
  try {
    await ElMessageBox.confirm(
      "把权限表和两个命令清单都改回出厂默认值,点「保存」后才生效。",
      "恢复默认",
      { type: "warning" }
    );
  } catch {
    return;
  }
  form.value = clone(data.value.defaults);
}

async function save() {
  if (!dirty.value) return;
  saving.value = true;
  try {
    const res = await savePermissionPolicy(form.value);
    if (!res?.success) {
      message(res?.message || "保存失败", { type: "error" });
      return;
    }
    if (res.data?.definitions) apply(res.data);
    else await load();
    message("已保存,立即生效", { type: "success" });
    // 当前账号页面上的按钮置灰也按新配置
    useUserStoreHook()
      .fetchMe()
      .catch(() => {});
  } catch {
    // 校验失败等错误由 http 统一弹出后端的提示
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<style lang="scss" scoped>
.system-permissions {
  padding: 4px;
}

.intro {
  padding-left: 16px;
  margin: 0;
  line-height: 1.7;
  list-style: disc;
}

.card-header {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-weight: 600;
}

.sub-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.op-name {
  display: flex;
  gap: 4px;
  align-items: center;

  .is-highlight {
    font-weight: 600;
    color: var(--el-color-danger);
  }
}

.hint-icon {
  margin-left: 2px;
  color: var(--el-text-color-secondary);
  vertical-align: -2px;
  cursor: help;
}

.add-command {
  display: flex;
  gap: 8px;
  width: 300px;
}
</style>

<style lang="scss">
.permission-hint {
  max-width: 420px;
  line-height: 1.6;
}
</style>
