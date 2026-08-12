#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
12306 余票监控脚本
==================
盯着指定车次(如 Z67 北京→哈尔滨西)的硬卧余票，
余票紧张/售罄/重新放票时，通过企业微信群机器人推送提醒到手机。

用法:
  python ticket_monitor.py            启动监控(默认读取 config.json)
  python ticket_monitor.py --once     只查一次,不循环
  python ticket_monitor.py --test     查询一次并发送测试推送

依赖: 仅 Python 标准库,无需安装任何第三方包。
"""

import json
import os
import random
import re
import sys
import time
import datetime
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "ticket_monitor.log")
STATION_CACHE = os.path.join(BASE_DIR, "station_codes.json")

BASE = "https://kyfw.12306.cn"
INIT_URL = BASE + "/otn/leftTicket/init"
QUERY_URL = BASE + "/otn/leftTicket/queryG"
STATION_URL = BASE + "/otn/resources/js/framework/station_name.js"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

# queryG 返回字段索引(以 | 分隔) —— 已对照 12306 官方前端 JS 实测验证:
#   queryLeftTicket_end_js.js 中:
#   dd.rw_num=c9[23](软卧) dd.rz_num=c9[24](软座) dd.wz_num=c9[26](无座)
#   dd.yw_num=c9[28](硬卧) dd.yz_num=c9[29](硬座)
#   dd.ze_num=c9[30](二等) dd.zy_num=c9[31](一等) dd.swz_num=c9[32](商务)
#   dd.seat_types=c9[35](席别代码列表) dd.yp_info_new=c9[39](票池信息)
IDX_TRAIN_CODE = 3
IDX_START_TIME = 8
IDX_ARRIVE_TIME = 9
IDX_DURATION = 10
IDX_SEAT_TYPES = 35
SEATS = {
    "商务座": 32,
    "一等座": 31,
    "二等座": 30,
    "硬座": 29,
    "硬卧": 28,
    "无座": 26,
    "特等座": 25,
    "软座": 24,
    "软卧": 23,
}
# 席别代码(12306 官方 seatTypeForHB 映射): 用于判断车次是否提供该席别
SEAT_TYPE_CODES = {
    "商务座": "9", "特等座": "P", "一等座": "M", "二等座": "O",
    "高级软卧": "6", "软卧": "4", "硬卧": "3", "软座": "2",
    "硬座": "1", "无座": "WZ",
}

# ==================== 日志 ====================

def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ==================== HTTP 层(标准库) ====================

def make_opener():
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return opener


def http_get(opener, url, timeout=15, referer=None, retries=3):
    """GET 请求,带随机 UA 与重试"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", random.choice(USER_AGENTS))
            req.add_header("Accept", "application/json, text/plain, */*")
            req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
            if referer:
                req.add_header("Referer", referer)
            resp = opener.open(req, timeout=timeout)
            raw = resp.read()
            # 兼容 BOM/utf-8/gbk(12306 有时返回带 BOM 的 UTF-8)
            for enc in ("utf-8-sig", "utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt + random.uniform(0, 1)
                log(f"请求失败({e}), {wait:.1f}秒后重试第{attempt + 2}次", "WARN")
                time.sleep(wait)
            else:
                raise
    return None


def init_session(opener):
    """先访问首页拿到 JSESSIONID 等 cookie,否则查询会被拦"""
    http_get(opener, INIT_URL, timeout=15)


def fetch_station_codes(opener, use_cache=True):
    """从 12306 官方静态文件拉取 站名->代码 映射,并缓存到本地(云端模式跳过写盘)"""
    if use_cache and os.path.exists(STATION_CACHE):
        try:
            with open(STATION_CACHE, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("ts", 0) > time.time() - 7 * 86400:
                return cached["codes"]
        except Exception:
            pass

    text = http_get(opener, STATION_URL, timeout=20)
    codes = {}
    # 格式: var station_names ='@bj|北京|BJP|beijing|bj|...@bjb|北京北|VAP|...'
    for item in text.split("@"):
        fields = item.split("|")
        if len(fields) >= 3 and fields[1] and fields[2]:
            codes[fields[1]] = fields[2]  # 中文名 -> 电报码
    if use_cache:
        with open(STATION_CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "codes": codes}, f, ensure_ascii=False)
    log(f"站点代码表已获取,共 {len(codes)} 个站")
    return codes


# ==================== 查询与解析 ====================

def query_tickets(opener, codes, train, date_str, from_stations, to_stations):
    """遍历出发/到达站组合,找到目标车次并返回座位信息"""
    results = []
    for fs in from_stations:
        fcode = codes.get(fs)
        if not fcode:
            log(f"未找到出发站代码: {fs}", "WARN")
            continue
        for ts in to_stations:
            tcode = codes.get(ts)
            if not tcode:
                log(f"未找到到达站代码: {ts}", "WARN")
                continue
            params = urllib.parse.urlencode({
                "leftTicketDTO.train_date": date_str,
                "leftTicketDTO.from_station": fcode,
                "leftTicketDTO.to_station": tcode,
                "purpose_codes": "ADULT",
            })
            url = QUERY_URL + "?" + params
            text = http_get(opener, url, referer=INIT_URL)
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                log(f"响应不是合法JSON(可能被拦截): {text[:120]}", "WARN")
                continue
            if not data.get("status"):
                log(f"查询接口返回异常: {json.dumps(data, ensure_ascii=False)[:200]}", "WARN")
                continue
            result = data.get("data", {}).get("result", []) or []
            for line in result:
                fields = line.split("|")
                if len(fields) < 32:
                    continue
                train_code = fields[IDX_TRAIN_CODE]
                # 匹配目标车次(支持 Z67 / Z67*/ Z6701 等写法)
                if not re.match(train + r"$", train_code) and not train_code.startswith(train):
                    continue
                seats = {}
                for name, idx in SEATS.items():
                    v = fields[idx] if idx < len(fields) else ""
                    seats[name] = v if v else ""
                seat_types = fields[IDX_SEAT_TYPES] if len(fields) > IDX_SEAT_TYPES else ""
                results.append({
                    "train": train_code,
                    "from": fs, "to": ts,
                    "start_time": fields[IDX_START_TIME],
                    "arrive_time": fields[IDX_ARRIVE_TIME],
                    "duration": fields[IDX_DURATION],
                    "seats": seats,
                    "seat_types": seat_types,
                    "raw_count": len(fields),
                })
                log(f"找到车次 {train_code}  {fs}->{ts}  "
                    f"发{fields[IDX_START_TIME]} 到{fields[IDX_ARRIVE_TIME]} 历时{fields[IDX_DURATION]}  "
                    f"席别[{seat_types}] "
                    f"软卧[{seats['软卧']}] 硬卧[{seats['硬卧']}] 硬座[{seats['硬座']}] "
                    f"软座[{seats['软座']}] 二等[{seats['二等座']}] 一等[{seats['一等座']}] 无座[{seats['无座']}]")
    return results


# ==================== 余票状态解析 ====================

def parse_seat(v):
    """把12306的余票字段翻译成可读状态
    '有'  -> 充足(返回 ('有', None))
    '无'  -> 售罄
    '候补'-> 仅候补
    '12'  -> 余票12张
    ''    -> 无信息(未开售/无此席别)
    """
    v = (v or "").strip()
    if not v:
        return ("无信息", None)
    if v == "有":
        return ("有票(充足)", None)
    if v == "无":
        return ("已售罄", 0)
    if v in ("候补", "0"):
        return ("仅候补", 0)
    if v.isdigit():
        return ("余票" + v + "张", int(v))
    return (v, None)


def judge_level(status_str, num, threshold, offered=True):
    """判定紧急程度: ok / tight / soldout / not_offered / unknown"""
    if not offered:
        return "not_offered"
    if status_str == "有票(充足)":
        return "ok"
    if status_str == "无信息":
        return "unknown"
    if status_str in ("已售罄",):
        return "soldout"
    if status_str == "仅候补":
        return "tight"
    if num is not None:
        if num == 0:
            return "soldout"
        if num <= threshold:
            return "tight"
        return "ok"
    return "unknown"


# ==================== 推送(企业微信群机器人) ====================

def push_wecom(webhook, content_md):
    """推送 markdown 消息到企业微信群机器人"""
    payload = json.dumps({
        "msgtype": "markdown",
        "markdown": {"content": content_md},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "curl/8.0")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    result = json.loads(body)
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信返回错误: {result}")
    return True


def push(config, content_md, title=None):
    p = config.get("push", {})
    ptype = p.get("type", "none")
    if ptype == "none":
        log("未配置推送渠道,仅记录日志", "WARN")
        return
    webhook = p.get("wecom_webhook", "").strip()
    if not webhook:
        log("推送类型为 wecom 但未配置 wecom_webhook", "WARN")
        return
    try:
        msg = "#### 12306余票提醒\n\n" + content_md
        push_wecom(webhook, msg)
        log("推送成功")
    except Exception as e:
        log(f"推送失败: {e}", "ERROR")


def build_status_text(cfg, results):
    """构造推送正文"""
    lines = []
    for r in results:
        seats = r["seats"]
        seat_name = cfg.get("seat", "硬卧")
        s, n = parse_seat(seats.get(seat_name, ""))
        # 该车次提供的席别(seat_types 里的代码->名称)
        offered_names = []
        st = r.get("seat_types", "")
        for name, code in SEAT_TYPE_CODES.items():
            if code in st:
                offered_names.append(name)
        offer_txt = "、".join(offered_names) if offered_names else "未知"
        lines.append(f"- **{r['train']}** {r['from']}->{r['to']}  "
                     f"{r['start_time']}发 次日{r['arrive_time']}到 历时{r['duration']}  "
                     f"〔提供:{offer_txt}〕")
        # 列出有值的席别
        shown = []
        for name in ("软卧", "硬卧", "软座", "硬座", "二等座", "一等座", "商务座", "无座"):
            v = seats.get(name, "")
            if v:
                shown.append(f"{name}:{v}")
        lines.append(f"  **{seat_name}: {s}**" + ((" | " + " ".join(shown)) if shown else ""))
    if not lines:
        lines.append(f"- 未查询到 {cfg['train']} 车次(可能未放票/已停运)")
    return "\n".join(lines)


# ==================== 主流程 ====================

def fetch_once(cfg, opener, codes):
    """单次查询,返回 (results, 最紧急级别, 状态描述)"""
    train = cfg["train"]
    date_str = cfg["date"]
    seat_name = cfg.get("seat", "硬卧")
    seat_code = SEAT_TYPE_CODES.get(seat_name, "")
    results = query_tickets(
        opener, codes, train, date_str,
        cfg.get("from_stations", ["北京"]),
        cfg.get("to_stations", ["哈尔滨西"]),
    )
    # 去重: 同一车次同一时刻只保留一条(12306 对邻近站会重复返回)
    seen_key = set()
    deduped = []
    for r in results:
        key = (r["train"], r["start_time"], r["arrive_time"])
        if key in seen_key:
            continue
        seen_key.add(key)
        deduped.append(r)
    results = deduped

    # 汇总最紧急状态
    worst = "ok"
    for r in results:
        s, n = parse_seat(r["seats"].get(seat_name, ""))
        offered = (not seat_code) or (seat_code in r.get("seat_types", ""))
        lv = judge_level(s, n, cfg.get("alert_threshold", 5), offered=offered)
        order = {"not_offered": 3, "soldout": 2, "tight": 1, "unknown": 0, "ok": -1}
        if order.get(lv, -1) > order.get(worst, -1):
            worst = lv
    if not results:
        worst = "unknown"
    desc = build_status_text(cfg, results)
    return results, worst, desc


STATE_FILE = os.path.join(BASE_DIR, "last_status.json")


def read_last_state():
    """云端模式: 读取上次运行的状态(跨运行去重提醒)"""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_last_state(level, desc):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"level": level, "ts": datetime.datetime.now().isoformat()}, f,
                      ensure_ascii=False, indent=1)
    except Exception as e:
        log(f"写入状态文件失败: {e}", "WARN")


def run_cloud(cfg, opener, codes):
    """云端单次模式: 状态存文件, 仅首次/状态变化时推送"""
    results, worst, desc = fetch_once(cfg, opener, codes)
    last = read_last_state()
    prev_level = last.get("level") if last else None
    print(f"[cloud] train={cfg['train']} date={cfg['date']} seat={cfg.get('seat')} level={worst} prev={prev_level}")
    print(desc)

    seat_name = cfg.get("seat", "硬卧")
    seat_txt = "无数据"
    for r in results:
        s, n = parse_seat(r["seats"].get(seat_name, ""))
        seat_txt = s

    if prev_level is None:
        push(cfg, f"**云监控已启动** 🛰️\n\n车次 {cfg['train']} 日期 {cfg['date']} {seat_name}\n\n" + desc)
        log("云端首次运行,已推送启动消息")
    elif worst != prev_level:
        msg = {
            "not_offered": f"⚠️ **该车次未提供{seat_name}席别** 当前:{seat_txt}",
            "tight": f"🚨 **{seat_name}紧张!** 当前:{seat_txt} 张",
            "soldout": f"⚠️ **{seat_name}已售罄** 当前:{seat_txt}",
            "ok": f"✅ **又有{seat_name}票了!** 当前:{seat_txt}",
            "unknown": f"❓ **状态未知** 当前:{seat_txt}",
        }.get(worst, f"**状态变化** 当前:{seat_txt}")
        push(cfg, msg + "\n\n" + desc)
        log(f"状态变化 {prev_level} -> {worst},已推送")
    else:
        log(f"状态未变化({worst}),不推送")

    write_last_state(worst, desc)
    return worst


def main():
    args = sys.argv[1:]
    once = "--once" in args
    test = "--test" in args
    cloud = "--cloud" in args

    if not os.path.exists(CONFIG_PATH):
        log(f"找不到配置文件 {CONFIG_PATH}", "ERROR")
        sys.exit(1)
    cfg = load_config()

    # 云端模式: 用环境变量覆盖配置, 状态持久化, 单次执行
    if cloud:
        cfg["train"] = os.environ.get("TRAIN", cfg["train"])
        cfg["date"] = os.environ.get("DATE", cfg["date"])
        cfg["seat"] = os.environ.get("SEAT", cfg.get("seat", "硬卧"))
        if os.environ.get("FROM_STATIONS"):
            cfg["from_stations"] = json.loads(os.environ["FROM_STATIONS"])
        if os.environ.get("TO_STATIONS"):
            cfg["to_stations"] = json.loads(os.environ["TO_STATIONS"])
        if os.environ.get("THRESHOLD"):
            cfg["alert_threshold"] = int(os.environ["THRESHOLD"])
        webhook = os.environ.get("WEBHOOK_URL", "").strip()
        if webhook:
            cfg["push"]["type"] = "wecom"
            cfg["push"]["wecom_webhook"] = webhook
        once = True

    # 日期合法性检查
    target = datetime.datetime.strptime(cfg["date"], "%Y-%m-%d").date()
    today = datetime.date.today()
    if target < today:
        log(f"目标日期 {cfg['date']} 已过,请修改 config.json 中的 date", "ERROR")
        sys.exit(1)
    days_left = (target - today).days

    log(f"启动监控: 车次{cfg['train']} 日期{cfg['date']}(还有{days_left}天) "
        f"区间{cfg.get('from_stations')}->{cfg.get('to_stations')} 席位:{cfg.get('seat', '硬卧')}")

    opener = make_opener()
    init_session(opener)
    codes = fetch_station_codes(opener, use_cache=not cloud)

    if cloud:
        run_cloud(cfg, opener, codes)
        return

    results, worst, desc = fetch_once(cfg, opener, codes)

    if test:
        push(cfg, "**测试消息**: 监控脚本已就绪,推送链路正常。\n\n" + desc)
        log("测试推送已发送(若未配置推送则仅打印)")
        return

    # 推送启动信息(含当前状态)
    push(cfg, f"**监控已启动**\n\n车次 {cfg['train']} 日期 {cfg['date']}\n\n" + desc)
    log("首次查询完成,进入监控循环")

    if once:
        return

    # ---- 状态机循环 ----
    last_level = worst
    last_heartbeat = time.time()
    interval = max(30, int(cfg.get("poll_interval_seconds", 180)))
    threshold = cfg.get("alert_threshold", 5)
    heartbeat_h = cfg.get("heartbeat_hours", 6)

    while True:
        time.sleep(interval + random.uniform(0, 20))  # 加抖动,降低被封概率
        try:
            opener = make_opener()
            init_session(opener)
            results, worst, desc = fetch_once(cfg, opener, codes)
        except Exception as e:
            log(f"查询异常: {e}, 等待下一轮", "ERROR")
            continue

        seat_name = cfg.get("seat", "硬卧")
        target_seat_info = "无数据"
        for r in results:
            s, n = parse_seat(r["seats"].get(seat_name, ""))
            target_seat_info = s

        now = time.time()
        # 心跳: 定时汇报一次当前状态
        if heartbeat_h and now - last_heartbeat > heartbeat_h * 3600:
            push(cfg, f"**定时汇报**({datetime.datetime.now():%H:%M})\n\n" + desc)
            last_heartbeat = now
            last_level = worst
            continue

        # 状态变化提醒
        if worst != last_level:
            if worst == "not_offered":
                push(cfg, f"⚠️ **该车次未提供{seat_name}席别** 当前:{target_seat_info}\n\n" + desc)
                log("触发:无此席别提醒")
            elif worst == "tight":
                push(cfg, f"🚨 **{seat_name}紧张!** 当前:{target_seat_info}\n\n" + desc)
                log("触发:席位紧张提醒")
            elif worst == "soldout":
                push(cfg, f"⚠️ **{seat_name}已售罄** 当前:{target_seat_info}\n\n" + desc)
                log("触发:售罄提醒")
            elif worst == "ok":
                push(cfg, f"✅ **又有{seat_name}票了!** 当前:{target_seat_info}\n\n" + desc)
                log("触发:重新放票提醒")
            elif worst == "unknown":
                push(cfg, f"❓ **状态未知** 当前:{target_seat_info}\n\n" + desc)
                log("触发:未知状态提醒")
            last_level = worst
        else:
            log(f"状态未变化: {seat_name} {target_seat_info}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("监控已手动停止")
        sys.exit(0)
