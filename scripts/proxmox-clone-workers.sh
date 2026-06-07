#!/bin/bash
# Proxmox: LXC コンテナ paprika-worker を連番で複製するスクリプト。
# Proxmox VE ノードの **shell (root)** で実行する。
#
# ※ paprika worker は QEMU VM ではなく **LXC コンテナ (CT)** なので pct を使う。
#   「VM 122」の実体は CT 122 = paprika-worker26。
#
# やること:
#   1. クラスタ全 guest を列挙して自動検出する:
#        - 複製元 CT (デフォルト CTID 122) のノード / 停止状態 / net0 / rootfs ストレージ
#        - 既存 paprika-worker<N> の最大番号 (= 連番の基点)
#        - 複製元 CT の net0 の IP / gw (= IP の基点)
#        - 使用済み VMID / 名前 / IP (衝突チェック用)
#   2. 指定台数(デフォルト 20)ぶんの複製プランを作る:
#        - hostname = 既存最大番号 +1, +2, …  (既存名の桁数・区切りを踏襲)
#        - IP       = 複製元 CT の net0 IP +1, +2, …  (同一サブネット内)
#        - CTID     = 既存最大 VMID の次の空き番号から連番
#   3. プランを表示 → 確認後に実行:
#        pct clone <src> <newid> --hostname <name> --full --storage <stg>
#        pct set   <newid> --net0 name=eth0,bridge=...,gw=...,ip=<ip>/<cidr>,type=veth
#          (hwaddr は付けない → 新しい MAC が自動採番され、重複しない)
#        (任意) pct start <newid>
#
#   ※ 複製元 rootfs はローカルストレージ(local-lvm)なので `pct clone --target` は
#     使えない(共有ストレージ限定)。クローンは複製元と同じノードに作られる。
#     別ノードへ散らしたい場合は後から停止状態で `pct migrate <ctid> <node>`。
#
# 使い方:
#   ./proxmox-clone-workers.sh                20台のプランを表示し、確認後に実行
#   ./proxmox-clone-workers.sh 5              5台だけ
#   ./proxmox-clone-workers.sh -n 30          30台
#   ./proxmox-clone-workers.sh --dry-run      プラン表示のみ(一切変更しない)
#   ./proxmox-clone-workers.sh -y             確認プロンプトを省略して即実行
#
# 環境変数で上書き:
#   SRC_VMID=122        複製元 CTID
#   COUNT=20            複製台数 (位置引数 / -n が優先)
#   WORKER_PREFIX=paprika-worker   既存ホスト名の接頭辞
#   START=0             1=複製後に各 CT を pct start する
#   STORAGE=            クローン先ストレージ (空=複製元 rootfs と同じ)
#   GATEWAY=            net0 の gw を明示 (空=複製元 net0 から継承)
#   PING_CHECK=1        1=割当前に候補IPへ ping し、生きている衝突を弾く
#   BASE_IP=            IP の基点を CIDR で明示 (例 10.10.50.164/24)。空=複製元 net0 の IP
#   ANCHOR_NUM=         hostname 連番の基点番号を明示。空=既存 worker の最大番号
#   PAD=                手動基点時の hostname ゼロ埋め桁数 (既定 1)
#
# 失敗時は途中で停止する。再実行すると、その時点の最大番号・最大IPの
# 続きから自然にレジュームする(作成済みのぶんはスキップされ番号が進む)。
set -euo pipefail

# ---- 設定(env で上書き可) -------------------------------------------------
SRC_VMID="${SRC_VMID:-122}"
COUNT="${COUNT:-20}"
WORKER_PREFIX="${WORKER_PREFIX:-paprika-worker}"
START="${START:-0}"
STORAGE="${STORAGE:-}"
GATEWAY="${GATEWAY:-}"
PING_CHECK="${PING_CHECK:-1}"
BASE_IP="${BASE_IP:-}"
ANCHOR_NUM="${ANCHOR_NUM:-}"
PAD="${PAD:-}"

DRY_RUN=0
ASSUME_YES=0

# 先頭のコメントブロック(shebang の次行〜最初の非コメント行の手前)を使い方として出す
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1{exit}' "$0"; }

# ---- 引数パース -------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--count) COUNT="$2"; shift 2 ;;
    --dry-run|--dry)  DRY_RUN=1; shift ;;
    -y|--yes)   ASSUME_YES=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; usage; exit 2 ;;
    *)  COUNT="$1"; shift ;;   # 位置引数 = 台数
  esac
done

[[ "$COUNT" =~ ^[0-9]+$ && "$COUNT" -ge 1 ]] || { echo "COUNT は 1 以上の整数で: '$COUNT'" >&2; exit 2; }

# ---- 前提チェック -----------------------------------------------------------
for bin in pvesh pct python3 ping; do
  command -v "$bin" >/dev/null 2>&1 || { echo "必須コマンドが見つからない: $bin (Proxmox VE ノードで実行してください)" >&2; exit 1; }
done
LOCAL_NODE="$(hostname -s 2>/dev/null || hostname)"

# ノード上でコマンドを実行(ローカルなら直接、別ノードなら pve の root-ssh 経由)
on_node() {
  local node="$1"; shift
  if [[ "$node" == "$LOCAL_NODE" ]]; then
    "$@"
  else
    local q; printf -v q ' %q' "$@"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "root@${node}" "$q"
  fi
}

# クラスタ全 guest を列挙。区切りは \x1f(空フィールドが潰れない非空白文字)。
# プログラムは -c で渡し、stdin は pvesh の JSON 専用にする(両方を stdin に
# するとプログラムが JSON を食い尽くして data が空になるため)。
list_guests() {
  pvesh get /cluster/resources --type vm --output-format json 2>/dev/null | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in data:
    print("\x1f".join(str(r.get(k, "")) for k in ("vmid", "name", "node", "type", "template")))
'
}

# 設定キーの値を取り出す: guest_cfg <node> <lxc|qemu> <vmid> <key>
guest_cfg() {
  pvesh get "/nodes/$1/$2/$3/config" --output-format json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(d.get(sys.argv[1], ""))
' "$4"
}

# net0 / ipconfig0 文字列から IPv4 を取り出す(ip=A.B.C.D[/N] → A.B.C.D)。
# IP が無いとき(DHCP/未設定の guest)は空を返す。set -e 下で var=$(ip_of ...) が
# 落ちないよう、必ず return 0 する。
ip_of() { [[ "$1" =~ ip=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) ]] && echo "${BASH_REMATCH[1]}"; return 0; }

echo "==> [1/4] クラスタの guest を列挙"
GUEST_LINES="$(list_guests)"
[[ -n "$GUEST_LINES" ]] || { echo "guest が見つからない (pvesh get /cluster/resources が空)" >&2; exit 1; }

# 使用済み集合 + 複製元情報 + worker 最大番号 を一度に拾う
USED_VMIDS=""; USED_NAMES=""
SRC_NODE=""; SRC_NAME=""; SRC_TYPE=""
MAX_VMID=0
ANCHOR_NAME=""; ANCHOR_FOUND_NUM=-1
GUEST_LIST=""   # vmid \x1f node \x1f type   (IP スキャン対象)

while IFS=$'\x1f' read -r vmid name node vtype template; do
  [[ -n "$vmid" ]] || continue
  USED_VMIDS+="${vmid}"$'\n'
  [[ -n "$name" ]] && USED_NAMES+="${name}"$'\n'
  [[ "$vmid" -gt "$MAX_VMID" ]] && MAX_VMID="$vmid"
  GUEST_LIST+="${vmid}"$'\x1f'"${node}"$'\x1f'"${vtype}"$'\n'

  if [[ "$vmid" == "$SRC_VMID" ]]; then
    SRC_NODE="$node"; SRC_NAME="$name"; SRC_TYPE="$vtype"
  fi
  # 既存 paprika-worker<N> の最大番号(LXC のみ)
  if [[ "$vtype" == "lxc" && "$name" =~ ^${WORKER_PREFIX}[-_\ ]?([0-9]+)$ ]]; then
    n=$((10#${BASH_REMATCH[1]}))
    if [[ "$n" -gt "$ANCHOR_FOUND_NUM" ]]; then
      ANCHOR_FOUND_NUM="$n"; ANCHOR_NAME="$name"
    fi
  fi
done <<< "$GUEST_LINES"

[[ -n "$SRC_NODE" ]] || { echo "複製元 CTID $SRC_VMID がクラスタに見つからない" >&2; exit 1; }

# ---- 複製元の素性を確認 -----------------------------------------------------
echo "==> [2/4] 複製元 CT $SRC_VMID を確認 (node=$SRC_NODE name='${SRC_NAME:-?}' type=$SRC_TYPE)"
if [[ "$SRC_TYPE" != "lxc" ]]; then
  echo "  !! このスクリプトは LXC コンテナ専用。CTID $SRC_VMID は type=$SRC_TYPE。" >&2
  echo "     paprika worker は LXC なので、複製元には LXC の CTID を指定してください。" >&2
  exit 1
fi

src_status="$(on_node "$SRC_NODE" pct status "$SRC_VMID" 2>/dev/null | awk '{print $2}' || true)"
if [[ "$src_status" == "running" ]]; then
  echo "  !! CT $SRC_VMID は稼働中。LXC は停止中(またはテンプレート)でないとクローンできない。" >&2
  echo "     'pct shutdown $SRC_VMID' で停止してから実行してください。" >&2
  exit 1
fi
echo "  - status=${src_status:-stopped}"

SRC_NET0="$(guest_cfg "$SRC_NODE" lxc "$SRC_VMID" net0)"
SRC_ROOTFS="$(guest_cfg "$SRC_NODE" lxc "$SRC_VMID" rootfs)"
[[ -n "$SRC_NET0" ]] || { echo "  !! 複製元 CT $SRC_VMID に net0 が無い。IP を設定できないため中止。" >&2; exit 1; }

# クローン先ストレージ: 明示が無ければ複製元 rootfs と同じ(例 "local-lvm:vm-122-disk-0" → "local-lvm")
[[ -n "$STORAGE" ]] || STORAGE="${SRC_ROOTFS%%:*}"
[[ -n "$STORAGE" ]] || { echo "  !! クローン先ストレージを決定できない。STORAGE= を指定してください。" >&2; exit 1; }

# net0 の基点(ip / gw)
SRC_IP="$(ip_of "$SRC_NET0")"
SRC_GW=""; [[ "$SRC_NET0" =~ gw=([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+) ]] && SRC_GW="${BASH_REMATCH[1]}"
SRC_CIDR="24"; [[ "$SRC_NET0" =~ ip=[0-9.]+/([0-9]+) ]] && SRC_CIDR="${BASH_REMATCH[1]}"

# ---- 基点(番号 / IP / GW)を決める -----------------------------------------
# IP の基点
if [[ -n "$BASE_IP" ]]; then BASE_IP_CIDR="$BASE_IP"
elif [[ -n "$SRC_IP" ]];  then BASE_IP_CIDR="${SRC_IP}/${SRC_CIDR}"
else echo "複製元 net0 から IP を読めない。BASE_IP=10.10.50.x/24 を指定してください。" >&2; exit 1; fi
# gw
BASE_GW="${GATEWAY:-$SRC_GW}"
[[ -n "$BASE_GW" ]] || { echo "ゲートウェイを決定できない。GATEWAY= を指定してください。" >&2; exit 1; }

# hostname 連番の基点
if [[ -n "$ANCHOR_NUM" ]]; then
  BASE_NUM="$ANCHOR_NUM"; NAME_PREFIX="$WORKER_PREFIX"; PAD_WIDTH="${PAD:-1}"
elif [[ "$ANCHOR_FOUND_NUM" -ge 0 ]]; then
  [[ "$ANCHOR_NAME" =~ ([0-9]+)$ ]] || { echo "anchor 名の解析に失敗: $ANCHOR_NAME" >&2; exit 1; }
  digits="${BASH_REMATCH[1]}"
  NAME_PREFIX="${ANCHOR_NAME%$digits}"; PAD_WIDTH="${#digits}"; BASE_NUM="$((10#$digits))"
else
  echo "既存 ${WORKER_PREFIX}<N> が見つからない。ANCHOR_NUM を指定して基点を与えてください。" >&2; exit 1
fi

# ---- IP 使用状況スキャン(全 guest の net0 / ipconfig0)----------------------
echo "==> [3/4] 既存 IP の使用状況をスキャン"
USED_IPS=""
gcount="$(printf '%s' "$GUEST_LIST" | grep -c . || true)"
scanned=0
while IFS=$'\x1f' read -r vmid node vtype; do
  [[ -n "$vmid" ]] || continue
  scanned=$((scanned+1))
  printf '\r  scanned %d/%d' "$scanned" "${gcount:-0}" >&2
  key="net0"; [[ "$vtype" == "qemu" ]] && key="ipconfig0"
  v="$(guest_cfg "$node" "$vtype" "$vmid" "$key")"
  ip="$(ip_of "$v")"
  [[ -n "$ip" ]] && USED_IPS+="${ip}"$'\n'
done <<< "$GUEST_LIST"
echo >&2

# ---- プランを計算(IP 演算・桁送り・衝突検査は python に集約)---------------
compute_plan() {
  python3 - <<'PY'
import os, sys, ipaddress

ipstr, _, cidr = os.environ["BASE_IP_CIDR"].partition("/")
cidr = cidr or "24"
try:
    base = ipaddress.ip_address(ipstr)
    net  = ipaddress.ip_network(f"{ipstr}/{cidr}", strict=False)
except Exception as e:
    sys.stderr.write(f"基点 IP の解析に失敗: {e}\n"); sys.exit(1)

gw        = os.environ["BASE_GW"]
prefix    = os.environ["NAME_PREFIX"]
pad       = int(os.environ["PAD_WIDTH"])
base_num  = int(os.environ["BASE_NUM"])
base_vmid = int(os.environ["BASE_VMID"])
count     = int(os.environ["COUNT"])

def s(name): return set(x for x in os.environ.get(name, "").splitlines() if x.strip())
used_names = s("USED_NAMES")
used_ips   = s("USED_IPS")
used_vmids = set(int(x) for x in os.environ.get("USED_VMIDS", "").splitlines() if x.strip().isdigit())

# 既存最大 VMID の次から、空いている VMID を count 個
vmids, v = [], base_vmid
while len(vmids) < count:
    v += 1
    if v not in used_vmids:
        vmids.append(v)

rows, errs = [], []
for i in range(1, count + 1):
    num   = base_num + i
    name  = f"{prefix}{num:0{pad}d}"
    newip = base + i
    bcast = net.broadcast_address if net.version == 4 else None
    if newip not in net or newip == net.network_address or newip == bcast:
        errs.append(f"IP {newip} が {net} の利用可能範囲外")
    if name in used_names:
        errs.append(f"ホスト名 {name} は既に存在")
    if str(newip) in used_ips:
        errs.append(f"IP {newip} は既に別 guest が使用中")
    rows.append((vmids[i - 1], name, f"{newip}/{cidr}", gw))

if errs:
    sys.stderr.write("プラン検査でエラー:\n" + "\n".join("  - " + e for e in errs) + "\n")
    sys.exit(1)
for r in rows:
    print("\t".join(str(x) for x in r))
PY
}

export BASE_IP_CIDR BASE_GW NAME_PREFIX PAD_WIDTH BASE_NUM COUNT USED_NAMES USED_IPS USED_VMIDS
export BASE_VMID="$MAX_VMID"
PLAN="$(compute_plan)" || exit 1

# net0 のひな型: 複製元 net0 から ip= と hwaddr= を外し、gw を基点 gw に揃える
# (実行時に ,ip=<新IP> を付け足す。hwaddr 無し → 新しい MAC が自動採番される)
NET0_BASE="$(SRC_NET0="$SRC_NET0" BASE_GW="$BASE_GW" python3 -c '
import os
toks = [t for t in os.environ["SRC_NET0"].split(",")
        if t and not t.startswith(("ip=", "hwaddr=", "gw="))]
toks.append("gw=" + os.environ["BASE_GW"])
print(",".join(toks))
')"

# ---- プラン表示 -------------------------------------------------------------
echo
echo "==> [4/4] 複製プラン"
echo "  複製元      : CT $SRC_VMID  (node=$SRC_NODE, name='${SRC_NAME}', status=${src_status:-stopped})"
echo "  クローン先  : node=$SRC_NODE  storage=$STORAGE  (ローカルストレージのため同一ノード固定)"
echo "  net0 ひな型 : ${NET0_BASE},ip=<各IP>"
echo "  基点        : worker番号=$BASE_NUM  IP=$BASE_IP_CIDR  gw=$BASE_GW"
echo "  台数        : $COUNT   起動=$([[ $START == 1 ]] && echo yes || echo no)"
echo
printf '   %-7s %-24s %-20s %s\n' "CTID" "HOSTNAME" "IP" "GATEWAY"
printf '   %-7s %-24s %-20s %s\n' "------" "------------------------" "------------------" "-----------"
while IFS=$'\t' read -r vmid name ipc gw; do
  [[ -n "$vmid" ]] || continue
  printf '   %-7s %-24s %-20s %s\n' "$vmid" "$name" "$ipc" "$gw"
done <<< "$PLAN"
echo

# ---- 候補 IP の死活チェック(任意)-----------------------------------------
if [[ "$PING_CHECK" == "1" ]]; then
  echo "==> 候補 IP の ping チェック"
  conflict=0
  while IFS=$'\t' read -r vmid name ipc gw; do
    [[ -n "$ipc" ]] || continue
    ip="${ipc%/*}"
    if ping -c1 -W1 "$ip" >/dev/null 2>&1; then
      echo "  !! $ip は応答あり(既に使用中の可能性)— $name" >&2
      conflict=1
    fi
  done <<< "$PLAN"
  if [[ "$conflict" == "1" ]]; then
    echo "  応答のある IP が見つかったため中止。PING_CHECK=0 で無効化できますが推奨しません。" >&2
    exit 1
  fi
  echo "  OK: 候補 IP はすべて無応答"
fi

if [[ "$DRY_RUN" == "1" ]]; then
  echo
  echo "（--dry-run のため、ここまで。CT は作成していません）"
  exit 0
fi

# ---- 確認 -------------------------------------------------------------------
if [[ "$ASSUME_YES" != "1" ]]; then
  echo
  read -r -p "上記 ${COUNT} 台を作成します。よろしいですか？ [yes と入力]: " ans || ans=""
  [[ "$ans" == "yes" ]] || { echo "中止しました。"; exit 1; }
fi

# ---- 実行 -------------------------------------------------------------------
created=()
trap 'echo; echo "==> 作成済み ${#created[@]} 台: ${created[*]:-なし}"' EXIT

i=0
while IFS=$'\t' read -r vmid name ipc gw; do
  [[ -n "$vmid" ]] || continue
  i=$((i+1))
  echo "==> [$i/$COUNT] clone $SRC_VMID -> $vmid  ($name  $ipc)"

  on_node "$SRC_NODE" pct clone "$SRC_VMID" "$vmid" \
    --hostname "$name" --full --storage "$STORAGE" \
    --description "paprika worker clone of CT ${SRC_VMID} (${SRC_NAME:-}) — $(date +%F)"
  on_node "$SRC_NODE" pct set "$vmid" --net0 "${NET0_BASE},ip=${ipc}"
  if [[ "$START" == "1" ]]; then
    on_node "$SRC_NODE" pct start "$vmid"
  fi

  created+=("${vmid}:${name}:${ipc%/*}")
done <<< "$PLAN"

echo
echo "==> 完了: ${#created[@]} 台を作成しました。"
[[ "$START" == "1" ]] || echo "   (未起動。'pct start <ctid>' で起動すると net0 の IP / hostname が反映されます)"
