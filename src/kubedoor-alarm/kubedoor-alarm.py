#!/usr/bin/python3
import json, requests, utils, silence
from flask import Flask, Response, request, jsonify
from psycopg_pool import ConnectionPool
from psycopg.rows import tuple_row
from datetime import datetime, UTC
import pytz
import logging
import hashlib
import urllib.parse

logging.basicConfig(level=getattr(logging, utils.LOG_LEVEL), format='%(asctime)s - %(levelname)s - %(message)s')
# PostgreSQL 连接池
_conninfo = (
    f"host={utils.PG_HOST} port={utils.PG_PORT} user={utils.PG_USER} "
    f"password={utils.PG_PASSWORD} dbname={utils.PG_DATABASE}"
)
pool = ConnectionPool(conninfo=_conninfo, min_size=1, max_size=10, open=True)

# 告警屏蔽引擎(内存缓存 alert_silences 规则,详见 silence.py)
silence.init_engine(pool)

MSG_TOKEN = utils.MSG_TOKEN
MSG_TYPE = utils.MSG_TYPE
DEFAULT_AT = utils.DEFAULT_AT
ALERTMANAGER_EXTURL = utils.ALERTMANAGER_EXTURL
KUBEDOOR_EXTURL = utils.KUBEDOOR_EXTURL
PROM_K8S_TAG_KEY = utils.PROM_K8S_TAG_KEY


def wecom(webhook, content, at):
    webhook = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=' + webhook
    headers = {'Content-Type': 'application/json'}
    params = {'msgtype': 'markdown', 'markdown': {'content': f"{content}<@{at}>"}}
    data = bytes(json.dumps(params), 'utf-8')
    response = requests.post(webhook, headers=headers, data=data)
    logging.info(f'【wecom】{response.json()}')


def dingding(webhook, content, at):
    webhook = 'https://oapi.dingtalk.com/robot/send?access_token=' + webhook
    headers = {'Content-Type': 'application/json'}
    params = {
        "msgtype": "markdown",
        "markdown": {"title": "告警", "text": content},
        "at": {"atMobiles": [at]},
    }
    data = bytes(json.dumps(params), 'utf-8')
    response = requests.post(webhook, headers=headers, data=data)
    logging.info(f'【dingding】{response.json()}')


def feishu(webhook, content, at):
    title = "告警通知"
    webhook = f'https://open.feishu.cn/open-apis/bot/v2/hook/{webhook}'
    headers = {'Content-Type': 'application/json'}
    params = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"{content}\n<at id={at}></at>",
                }
            ],
        },
    }
    data = json.dumps(params)
    response = requests.post(webhook, headers=headers, data=data)
    logging.info(f'【feishu】{response.json()}')


def slack(webhook, content, at=""):
    """发送Slack告警通知"""
    # 构建完整的Slack Webhook URL
    webhook_url = f'https://hooks.slack.com/services/{webhook}'
    headers = {'Content-Type': 'application/json'}

    # 遍历消息列表，每个消息都发送一条通知
    for message in content:
        # 构建blocks
        blocks = []

        # 第一个block：标题（加粗）
        title_block = {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": message[0], "style": {"bold": True}}],
                }
            ],
        }
        blocks.append(title_block)

        # 第二个block：消息内容（包含@用户）
        content_text = message[1]
        if at:
            content_text += f" <@{at}>"

        content_block = {"type": "section", "text": {"type": "mrkdwn", "text": content_text}}
        blocks.append(content_block)

        # 如果有第三个字段（告警情况），添加屏蔽链接block
        if len(message) >= 3:
            link_block = {
                "type": "rich_text",
                "elements": [
                    {"type": "rich_text_section", "elements": [{"type": "link", "text": "【屏蔽】", "url": message[2]}]}
                ],
            }
            blocks.append(link_block)

        # 添加分隔线
        divider_block = {"type": "divider"}
        blocks.append(divider_block)

        params = {"blocks": blocks}

        data = json.dumps(params)
        response = requests.post(webhook_url, headers=headers, data=data)

        logging.info(f'【slack】status_code: {response.status_code}, text: {response.text}')


def parse_alert_time(time_str):
    """将Alertmanager的时间字符串转换为上海时区的DateTime对象"""
    time_str = time_str[:19] + 'Z'
    utc_time = datetime.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
    utc_time = utc_time.replace(tzinfo=pytz.UTC)
    return utc_time.astimezone(pytz.timezone('Asia/Shanghai'))


def extract_container_from_pod(pod_name):
    """从pod名称中提取container名称"""
    try:
        import re

        # Kubernetes pod命名规则:
        # 1. Deployment: <deployment-name>-<replicaset-hash>-<pod-hash>
        # 2. ReplicaSet: <replicaset-name>-<pod-hash>
        # hash通常是5-10位的随机字符串（字母数字组合）

        # 使用正则表达式匹配末尾的hash模式
        # 匹配最后一个或两个hash（5-10位字母数字）
        pattern = r'^(.+?)-[a-z0-9]{5,10}(-[a-z0-9]{5,10})?$'
        match = re.match(pattern, pod_name)

        if match:
            return match.group(1)

        # 如果正则匹配失败，使用备用方案
        # 去掉最后1-2个看起来像hash的部分
        parts = pod_name.split('-')
        if len(parts) >= 2:
            # 检查最后一部分是否像hash（5-10位字母数字）
            if len(parts[-1]) >= 5 and len(parts[-1]) <= 10 and parts[-1].isalnum():
                # 检查倒数第二部分是否也像hash
                if len(parts) >= 3 and len(parts[-2]) >= 5 and len(parts[-2]) <= 10 and parts[-2].isalnum():
                    return '-'.join(parts[:-2])
                else:
                    return '-'.join(parts[:-1])

        # 如果所有解析方法都失败，强制去掉最后两部分
        parts = pod_name.split('-')
        if len(parts) >= 3:
            return '-'.join(parts[:-2])
        elif len(parts) >= 2:
            return '-'.join(parts[:-1])
        else:
            return ''
    except:
        return ''


def process_single_alert(alert):
    try:
        # 解析时间
        starts_at = parse_alert_time(alert['startsAt'])
        ends_at = parse_alert_time(alert['endsAt'])

        # 格式化时间字符串
        start_time_str = starts_at.strftime("%Y-%m-%d %H:%M:%S")
        end_time_str = ends_at.strftime("%Y-%m-%d %H:%M:%S")

        # 解析标签和注解
        labels = alert.get('labels', {})
        annotations = alert.get('annotations', {})
        description = annotations.get('description', '').split('\n- ')[-1]

        # 提取K8s相关字段，支持备用字段
        namespace = labels.get('namespace', '') or labels.get('k8s_ns', '')
        pod = labels.get('pod', '') or labels.get('k8s_pod', '')
        container = labels.get('container', '') or labels.get('k8s_app', '')
        env = labels.get(PROM_K8S_TAG_KEY, '')
        alert_name = labels.get('alertname', '')

        # 生成指纹
        fingerprint_str = env + namespace + pod + alert_name
        fingerprint = hashlib.md5(fingerprint_str.encode(encoding='UTF-8')).hexdigest()
        promfinger = alert['fingerprint']

        alert_data = {
            'promfinger': promfinger,
            'fingerprint': fingerprint,
            'start_time': start_time_str,
            'end_time': end_time_str,
            'severity': labels.get('severity', ''),
            'alert_group': labels.get('alertgroup', ''),
            'alert_name': alert_name,
            'env': env,
            'namespace': namespace,
            'container': container,
            'pod': pod,
            'description': description,
        }
        send_resolved = False if labels.get('send_resolved', True) == 'false' else True

        # 屏蔽判断:命中规则的告警仍然入库(带 silenced 标记),只是不会走通知路径。
        # 通知路径 /msg 会独立判断并计数,这里不重复累加 match_count。
        silence_id = silence.match_alert(labels, annotations, count_hit=False)
        if silence_id:
            logging.info(f"告警命中屏蔽规则 #{silence_id},入库但标记为已屏蔽: {alert_name}")

        if alert['status'] == 'firing':
            handle_firing_alert(alert_data, send_resolved, silence_id)
        else:
            handle_resolved_alert(alert_data, send_resolved, silence_id)

    except Exception as e:
        logging.error(f"处理告警失败: {str(e)}", exc_info=True)


def handle_firing_alert(alert_data, send_resolved, silence_id=None):
    """处理 firing 告警:当天同 fingerprint 已存在则累加计数,否则插入新记录。

    用 INSERT ... ON CONFLICT (start_time, fingerprint) 需要精确匹配唯一键;
    但这里的去重语义是「当天(start_time::date)+fingerprint」,与唯一索引
    (start_time, fingerprint) 不完全一致,故沿用「先查后改/插」逻辑,改为 PG 语法。

    silenced/silence_id 采用覆盖写,反映"最近一次告警是否被屏蔽",
    这样解除屏蔽后新来的告警会把当天记录改回未屏蔽状态。
    """
    day = alert_data['start_time'].split()[0]
    silenced = silence_id is not None
    with pool.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM k8s_pod_alert_days "
            "WHERE start_time::date = %s AND fingerprint = %s LIMIT 1",
            (day, alert_data['fingerprint']),
        ).fetchone()

        if existing:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE k8s_pod_alert_days SET "
                "count_firing = count_firing + 1, end_time = %s, "
                "alert_status = 'firing', operate = '未处理', description = %s, "
                "silenced = %s, silence_id = %s "
                "WHERE start_time::date = %s AND fingerprint = %s",
                (
                    current_time, alert_data['description'], silenced, silence_id,
                    day, alert_data['fingerprint'],
                ),
            )
            logging.info(f"更新告警计数: {alert_data['fingerprint']}: {alert_data['alert_name']}")
        else:
            count_resolved = 0 if send_resolved else -1
            conn.execute(
                "INSERT INTO k8s_pod_alert_days ("
                "fingerprint, alert_status, send_resolved, operate, "
                "start_time, count_firing, count_resolved, "
                "severity, alert_group, alert_name, env, namespace, "
                "container, pod, description, silenced, silence_id"
                ") VALUES (%s,'firing',%s,'未处理',%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    alert_data['fingerprint'], send_resolved, alert_data['start_time'],
                    count_resolved, alert_data['severity'], alert_data['alert_group'],
                    alert_data['alert_name'], alert_data['env'], alert_data['namespace'],
                    alert_data['container'], alert_data['pod'], alert_data['description'],
                    silenced, silence_id,
                ),
            )
            logging.info(f"新建告警记录: {alert_data['fingerprint']}: {alert_data['alert_name']}")
    return True, ''


def handle_resolved_alert(alert_data, send_resolved, silence_id=None):
    if not send_resolved:
        err = f"告警 {alert_data['fingerprint']}: {alert_data['alert_name']} 的 send_resolved 为 false，不入库"
        logging.warning(err)
        return False, err

    day = alert_data['start_time'].split()[0]
    silenced = silence_id is not None
    with pool.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM k8s_pod_alert_days "
            "WHERE start_time::date = %s AND fingerprint = %s LIMIT 1",
            (day, alert_data['fingerprint']),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE k8s_pod_alert_days SET "
                "alert_status = 'resolved', end_time = %s, "
                "count_resolved = count_resolved + 1, description = %s, "
                "silenced = %s, silence_id = %s "
                "WHERE start_time::date = %s AND fingerprint = %s",
                (
                    alert_data['end_time'], alert_data['description'], silenced, silence_id,
                    day, alert_data['fingerprint'],
                ),
            )
            logging.info(f"标记告警解决: {alert_data['fingerprint']}: {alert_data['alert_name']}")
            return True, ''
        else:
            err = f"未找到对应告警记录: {alert_data['fingerprint']}: {alert_data['alert_name']}"
            logging.error(err)
            return False, err


app = Flask(__name__)


# Alertmanager 的入库 webhook:把 Pod 类告警写进 PostgreSQL 的 k8s_pod_alert_days
@app.route('/alert/store', methods=['POST'])
def handle_alert():
    try:
        data = request.get_json()
        if not data or 'alerts' not in data:
            return jsonify({'status': 'error', 'message': '无效的请求格式'}), 400

        for alert in data['alerts']:
            logging.debug(str(alert))
            process_single_alert(alert)

        return jsonify({'status': 'success', 'message': '告警处理完成'}), 200

    except Exception as e:
        logging.error(f"处理请求时发生异常: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/custom_alert', methods=['POST'])
def handle_custom_alert():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'status': 'error', 'message': '无效的请求格式'}), 400

        # 验证必需字段
        required_fields = [
            'start_time',
            'end_time',
            'severity',
            'alert_group',
            'alert_name',
            'env',
            'namespace',
            'pod',
            'description',
            'send_resolved',
            'alert_status',
        ]

        for field in required_fields:
            if field not in data:
                return jsonify({'status': 'error', 'message': f'缺少必需字段: {field}'}), 400

        # 验证severity值
        valid_severities = ['Critical', 'Info', 'Notice', 'Warning']
        if data['severity'] not in valid_severities:
            return (
                jsonify({'status': 'error', 'message': f'severity必须是以下值之一: {valid_severities}'}),
                400,
            )

        # 验证alert_status值
        valid_statuses = ['firing', 'resolved']
        if data['alert_status'] not in valid_statuses:
            return (
                jsonify(
                    {
                        'status': 'error',
                        'message': f'alert_status必须是以下值之一: {valid_statuses}',
                    }
                ),
                400,
            )

        # 验证时间格式
        try:
            datetime.strptime(data['start_time'], "%Y-%m-%d %H:%M:%S")
            datetime.strptime(data['end_time'], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({'status': 'error', 'message': '时间格式必须为: %Y-%m-%d %H:%M:%S'}), 400

        # 处理container字段：如果用户传了就用用户传的，如果没传就从pod名称中截取
        if 'container' in data and data['container']:
            container = data['container']
        else:
            container = extract_container_from_pod(data['pod'])

        # 生成指纹
        fingerprint_str = data['env'] + data['namespace'] + data['pod'] + data['alert_name']
        fingerprint = hashlib.md5(fingerprint_str.encode(encoding='UTF-8')).hexdigest()

        # 构造alert_data
        alert_data = {
            'fingerprint': fingerprint,
            'start_time': data['start_time'],
            'end_time': data['end_time'],
            'severity': data['severity'],
            'alert_group': data['alert_group'],
            'alert_name': data['alert_name'],
            'env': data['env'],
            'namespace': data['namespace'],
            'container': container,
            'pod': data['pod'],
            'description': data['description'],
        }

        send_resolved = data['send_resolved']
        alert_status = data['alert_status']

        # 屏蔽判断:自定义告警没有原始 Alertmanager labels,用规范化字段构造匹配上下文
        silence_id = silence.match_alert(silence.build_match_labels_from_record(alert_data), count_hit=True)
        if silence_id:
            logging.info(f"自定义告警命中屏蔽规则 #{silence_id}: {alert_data['alert_name']}")

        # 根据alert_status调用相应的处理函数
        if alert_status == 'firing':
            result, msg = handle_firing_alert(alert_data, send_resolved, silence_id)
        else:
            result, msg = handle_resolved_alert(alert_data, send_resolved, silence_id)
        if result:
            return jsonify({'status': 'success', 'message': '自定义告警处理完成', 'silenced': bool(silence_id)}), 200
        else:
            return jsonify({'status': 'error', 'message': msg}), 400
    except Exception as e:
        logging.error(f"处理自定义告警时发生异常: {str(e)}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


def build_silence_url(labels):
    """构造通知里【屏蔽】链接。

    配置了 KUBEDOOR_EXTURL 就指向 KubeDoor 的屏蔽管理页,并把当前告警的关键标签
    通过 prefill 参数带过去,页面会自动打开新建弹窗并预填匹配条件;
    未配置时退回原来的 Alertmanager 链接。
    """
    alertname = labels.get('alertname', '')
    if not KUBEDOOR_EXTURL:
        quoted = urllib.parse.quote(f'{{alertname="{alertname}"}}')
        return f"{ALERTMANAGER_EXTURL}/#/alerts?silenced=false&inhibited=false&active=true&filter={quoted}"

    prefill = {'alertname': alertname}
    for canonical, candidates in (
        ('env', (PROM_K8S_TAG_KEY,)),
        ('namespace', ('namespace', 'k8s_ns')),
        ('pod', ('pod', 'k8s_pod')),
    ):
        for candidate in candidates:
            if candidate and labels.get(candidate):
                prefill[canonical] = labels[candidate]
                break
    quoted = urllib.parse.quote(json.dumps(prefill, ensure_ascii=False))
    return f"{KUBEDOOR_EXTURL.rstrip('/')}/#/alarm/silence?prefill={quoted}"


@app.route("/msg/<path:token>", methods=['POST'])
def alertnode(token):
    req = request.get_json()
    logging.info('↓↓↓↓↓↓↓↓↓↓↓↓↓↓node↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓↓')
    logging.info(json.dumps(req, indent=2, ensure_ascii=False))
    logging.info('↑↑↑↑↑↑↑↑↑↑↑↑↑↑node↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑↑')
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    now_cn = datetime.now()

    # time1830 = datetime.strptime(str(now_cn.date()) + '18:30', '%Y-%m-%d%H:%M')
    # time0830 = datetime.strptime(str(now_cn.date()) + '08:30', '%Y-%m-%d%H:%M')

    # if (now_cn > time1830 or now_cn < time0830):
    #    return Response(status=204)
    im, key = token.split('=', 1)
    logging.info(f"im: {im}, key: {key}")
    if im == 'slack':
        allmd = []
    else:
        allmd = ''
    at = DEFAULT_AT
    silenced_count = 0
    for i in req["alerts"]:
        # 屏蔽判断:命中规则的告警直接跳过通知(故障与恢复一并屏蔽,避免只收到恢复消息)
        silence_id = silence.match_alert(i.get('labels', {}), i.get('annotations', {}), count_hit=True)
        if silence_id:
            silenced_count += 1
            logging.info(
                f"【silence】已屏蔽通知 规则#{silence_id}: "
                f"{i.get('labels', {}).get('alertname', '')} status={i.get('status', '')}"
            )
            continue

        status = "故障" if i['status'] == "firing" else "恢复"
        try:
            firstime = datetime.strptime(i['startsAt'], '%Y-%m-%dT%H:%M:%SZ')
            durn_s = (now_utc - firstime).total_seconds()
        except:
            try:
                firstime = datetime.strptime(i['startsAt'], '%Y-%m-%dT%H:%M:%S.%fZ')
                durn_s = (now_utc - firstime).total_seconds()
            except:
                firstime = datetime.strptime(i['startsAt'].split(".")[0], '%Y-%m-%dT%H:%M:%S+08:00')
                durn_s = (now_cn - firstime).total_seconds()
        if durn_s < 60:
            durninfo = '小于1分钟'
        elif durn_s < 3600:
            durn = round(durn_s / 60, 1)
            durninfo = f"已持续{durn}分钟"
        else:
            durn = round(durn_s / 3600, 1)
            durninfo = f"已持续{durn}小时"

        summary = f"{i['labels']['alertname']},{durninfo}"
        message = i['annotations']['description']
        at = i['annotations'].get('at', DEFAULT_AT)

        url = build_silence_url(i['labels'])

        if im == 'slack':
            if status == '恢复':
                info = [f'🎉{status}: {summary}', message]
            else:
                info = [f'💥{status}: {summary}', message, url]
            allmd.append(info)
        else:
            if status == '恢复':
                info = f"### {status}<font color=\"#6aa84f\">{summary}</font>\n- {message}\n\n"
            else:
                info = f"### {status}<font color=\"#ff0000\">{summary}</font>\n- {message}[【屏蔽】]({url})\n\n"
            allmd = allmd + info

    if not allmd:
        logging.info(f"本批 {silenced_count} 条告警全部被屏蔽，不发送通知")
        return Response(status=204)
    if silenced_count:
        logging.info(f"本批告警中 {silenced_count} 条被屏蔽，其余正常通知")

    if im == 'wecom':
        wecom(key, allmd, at)
    elif im == 'dingding':
        dingding(key, allmd, at)
    elif im == 'feishu':
        feishu(key, allmd, at)
    elif im == 'slack':
        slack(key, allmd, at)
    return Response(status=200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
