var MAX_LABEL_CHARS = 32
var MAX_TOOLTIP_CHARS = 240
var MAX_SNAP_BYTES = 65536

function trim(s) {
  return String(s || "").replace(/^\s+|\s+$/g, "")
}

function asPlainUi(value, maxLen) {
  var s = String(value || "")
  s = s.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, "")
  s = s.replace(/[\u202A-\u202E\u2066-\u2069]/g, "")
  s = s.replace(/[<>&]/g, "")
  s = s.replace(/[ \t]+/g, " ")
  s = trim(s)
  var cap = parseInt(maxLen, 10)
  if (!isFinite(cap) || cap <= 0) cap = 80
  if (s.length > cap) s = s.slice(0, cap)
  return s
}

function withinByteCap(raw, maxBytes) {
  var s = String(raw || "")
  return s.length > 0 && s.length < maxBytes
}

function parseSnap(raw) {
  if (!withinByteCap(raw, MAX_SNAP_BYTES)) return null
  try {
    var data = JSON.parse(String(raw || "{}"))
    if (!data || typeof data !== "object") return null
    return data
  } catch (e) {
    return null
  }
}

function num(v, fallback) {
  var n = Number(v)
  return isFinite(n) ? n : (fallback || 0)
}

function pct(v) {
  return Math.round(num(v, 0)) + "%"
}

function deg(v) {
  if (v === null || v === undefined || v === "") return "—"
  var n = Number(v)
  if (!isFinite(n)) return "—"
  return Math.round(n) + "°"
}

function watts(v) {
  if (v === null || v === undefined || v === "") return "—"
  var n = Number(v)
  if (!isFinite(n)) return "—"
  if (n >= 100) return Math.round(n) + " W"
  var s = n.toFixed(1)
  if (s.slice(-2) === ".0") s = s.slice(0, -2)
  return s + " W"
}

function tempWatts(temp, w, estimate) {
  var t = deg(temp)
  var p = watts(w)
  if (p === "—") return t
  if (estimate) p = "~" + p
  if (t === "—") return p
  return t + " · " + p
}

function stressed(snap, limit) {
  if (!snap || !snap.reachable) return false
  var lim = Number(limit)
  if (!isFinite(lim) || lim <= 0) lim = 80
  var cpu = num((snap.cpu || {}).percent)
  var gpu = num((snap.gpu || {}).percent)
  return cpu >= lim || gpu >= lim
}

function barLabel(snap) {
  if (!snap || !snap.reachable) return "omaserver off"
  if (stressed(snap)) return "omaserver stress"
  return "omaserver"
}

function diskSpace(disk) {
  var d = disk || {}
  var used = num(d.used_gb)
  var total = num(d.total_gb)
  if (total >= 1000) {
    var usedStr = used >= 1000 ? (used / 1024).toFixed(1) + " T" : Math.round(used) + " G"
    return usedStr + " / " + (total / 1024).toFixed(1) + " T"
  }
  return used + "/" + total + " G"
}

function diskExtra(disk) {
  return diskSpace(disk)
}

function ramKind(mem) {
  var m = mem || {}
  var kind = String(m.kind || "")
  var mt = Number(m.mt_s)
  if (kind && isFinite(mt) && mt > 0) return kind + "-" + Math.round(mt)
  if (isFinite(mt) && mt > 0) return String(Math.round(mt))
  return kind
}

function ramExtra(mem) {
  var m = mem || {}
  return (m.used_gb || 0) + "/" + (m.total_gb || 0) + " G"
}

function uptime(seconds) {
  var n = Number(seconds)
  if (!isFinite(n) || n <= 0) return ""
  var d = Math.floor(n / 86400)
  var h = Math.floor((n % 86400) / 3600)
  var m = Math.floor((n % 3600) / 60)
  if (d > 0) return d + "d " + h + "h"
  if (h > 0) return h + "h " + m + "m"
  return Math.max(1, m) + "m"
}

function biosLine(bios) {
  var b = bios || {}
  var version = trim(b.version)
  var date = trim(b.date)
  if (!version && !date) return ""
  return "BIOS " + version + (date ? " · " + date : "")
}

function sysLine(snap) {
  if (!snap) return ""
  var bits = []
  var kernel = trim(snap.kernel)
  if (kernel) bits.push("kernel " + kernel)
  var up = uptime(snap.uptime_s)
  if (up) bits.push("up " + up)
  return bits.join(" · ")
}

function hwLine(cpu, mem) {
  var c = cpu || {}
  var m = mem || {}
  var bits = []
  var threads = num(c.cores, 0)
  if (threads > 0) bits.push(Math.round(threads) + " threads")
  var total = num(m.total_gb, 0)
  if (total > 0) bits.push(total + " GB RAM")
  return bits.join(" · ")
}

function dockerList(snap) {
  if (!snap || !snap.docker) return []
  var items = snap.docker.items
  if (!items) return []
  var out = []
  var n = items.length
  if (typeof n !== "number") return []
  for (var i = 0; i < n; i++) {
    var c = items[i]
    if (!c) continue
    out.push({
      name: String(c.name || ""),
      dot: String(c.dot || "off"),
      status: String(c.status || ""),
      web: String(c.web || "")
    })
  }
  return out
}

function dockerDotColor(dot) {
  var d = String(dot || "")
  if (d === "ok") return "#8FDB4F"
  if (d === "problem") return "#FFB020"
  return "#E23B3B"
}

function tooltip(snap) {
  if (!snap) return "omaserver"
  if (!snap.reachable) return asPlainUi("omaserver offline" + (snap.error ? " · " + snap.error : ""), MAX_TOOLTIP_CHARS)
  var cpu = snap.cpu || {}
  var mem = snap.mem || {}
  var gpu = snap.gpu || {}
  var disk = snap.disk || {}
  var bits = []
  bits.push("CPU " + pct(cpu.percent) + " " + deg(cpu.temp))
  bits.push("RAM " + pct(mem.percent))
  bits.push("GPU " + pct(gpu.percent) + " " + deg(gpu.temp))
  bits.push("DISK " + pct(disk.percent) + " " + diskExtra(disk))
  var pw = snap.power || {}
  if (pw.watts !== null && pw.watts !== undefined && pw.watts !== "") bits.push("PWR " + watts(pw.watts))
  var dock = (snap.docker && snap.docker.items) ? snap.docker.items.length : 0
  if (dock) bits.push("DOCKER " + dock)
  return asPlainUi(bits.join(" · "), MAX_TOOLTIP_CHARS)
}

if (typeof module !== "undefined") {
  module.exports = {
    parseSnap: parseSnap,
    barLabel: barLabel,
    tooltip: tooltip,
    asPlainUi: asPlainUi,
    pct: pct,
    deg: deg,
    watts: watts,
    tempWatts: tempWatts,
    num: num,
    diskExtra: diskExtra,
    diskSpace: diskSpace,
    ramExtra: ramExtra,
    ramKind: ramKind,
    uptime: uptime,
    biosLine: biosLine,
    sysLine: sysLine,
    hwLine: hwLine,
    dockerDotColor: dockerDotColor,
    dockerList: dockerList,
    stressed: stressed
  }
}
