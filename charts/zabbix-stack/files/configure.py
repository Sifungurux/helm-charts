#!/usr/bin/env python3
"""Make a Zabbix 7.0 instance match the desired state in state.json.

Runs as a Kubernetes Job from the zabbix-stack chart. Standard library only.

Idempotent: every object is looked up by name and created or updated. Only
objects this script manages are touched. Hosts it created carry the tag
managed-by=zabbix-stack; such hosts that are no longer in the desired state are
disabled, never deleted.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MANAGED_TAG = {"tag": "managed-by", "value": "zabbix-stack"}
DISCORD_ACTION = "zabbix-stack: Discord"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Interface types
IF_AGENT, IF_SNMP = 1, 2
# Item types / value types
ITEM_ZABBIX_ACTIVE, ITEM_DEPENDENT, ITEM_SCRIPT = 7, 18, 21
VALUE_FLOAT, VALUE_UNSIGNED, VALUE_TEXT = 0, 3, 4
# Macro types
MACRO_TEXT, MACRO_SECRET = 0, 1
# Trigger priorities
WARNING, AVERAGE, HIGH = 2, 3, 4


def log(msg):
    print(f"[configure] {msg}", flush=True)


class ApiError(Exception):
    pass


class Api:
    def __init__(self, url):
        self.url = url
        self.token = None
        self._id = 0

    def call(self, method, params=None, auth=True):
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params if params is not None else {}, "id": self._id}
        ).encode()
        headers = {"Content-Type": "application/json-rpc"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            reply = json.loads(resp.read().decode())
        if "error" in reply:
            err = reply["error"]
            raise ApiError(f"{method}: {err.get('message')} {err.get('data')}")
        return reply["result"]


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
SECRETS_DIR = os.environ.get("SECRETS_DIR", "/secrets")


def secret(key, required=True):
    path = os.path.join(SECRETS_DIR, key)
    try:
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
    except FileNotFoundError:
        value = ""
    if not value and required:
        raise SystemExit(f"secret key '{key}' is missing or empty")
    return value


def wait_for_file(path, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        log(f"waiting for {path}")
        time.sleep(10)
    raise SystemExit(f"{path} did not appear within {timeout}s")


# --------------------------------------------------------------------------- #
# Connection and login
# --------------------------------------------------------------------------- #
def wait_for_api(api, timeout=1500):
    deadline = time.time() + timeout
    while True:
        try:
            version = api.call("apiinfo.version", auth=False)
            log(f"Zabbix API {version} at {api.url}")
            return version
        except (urllib.error.URLError, OSError, ApiError, ValueError) as exc:
            if time.time() > deadline:
                raise SystemExit(f"API not reachable: {exc}")
            log(f"API not ready yet ({exc}); retrying")
            time.sleep(10)


def login(api, rotate):
    wanted = secret("admin-password")
    try:
        api.token = api.call("user.login", {"username": "Admin", "password": wanted}, auth=False)
        log("logged in as Admin")
        return
    except ApiError as exc:
        if not rotate:
            raise SystemExit(f"login failed: {exc}")
        log("login with admin-password failed; trying the default password to rotate it")
    api.token = api.call("user.login", {"username": "Admin", "password": "zabbix"}, auth=False)
    users = api.call("user.get", {"filter": {"username": "Admin"}, "output": ["userid"]})
    api.call("user.update", {"userid": users[0]["userid"], "current_passwd": "zabbix", "passwd": wanted})
    log("rotated the Admin password")
    api.token = api.call("user.login", {"username": "Admin", "password": wanted}, auth=False)


# --------------------------------------------------------------------------- #
# Small lookup helpers
# --------------------------------------------------------------------------- #
class Zabbix:
    def __init__(self, api):
        self.api = api
        self._groups = {}
        self._tgroups = {}
        self._templates = {}

    def hostgroup(self, name):
        if name not in self._groups:
            found = self.api.call("hostgroup.get", {"filter": {"name": [name]}, "output": ["groupid"]})
            if found:
                self._groups[name] = found[0]["groupid"]
            else:
                self._groups[name] = self.api.call("hostgroup.create", {"name": name})["groupids"][0]
                log(f"created host group {name}")
        return self._groups[name]

    def templategroup(self, name):
        if name not in self._tgroups:
            found = self.api.call("templategroup.get", {"filter": {"name": [name]}, "output": ["groupid"]})
            if found:
                self._tgroups[name] = found[0]["groupid"]
            else:
                self._tgroups[name] = self.api.call("templategroup.create", {"name": name})["groupids"][0]
                log(f"created template group {name}")
        return self._tgroups[name]

    def template_ids(self, names):
        missing = [n for n in names if n not in self._templates]
        if missing:
            for t in self.api.call("template.get", {"filter": {"host": missing}, "output": ["templateid", "host"]}):
                self._templates[t["host"]] = t["templateid"]
        unknown = [n for n in names if n not in self._templates]
        if unknown:
            raise ApiError(f"templates not found: {', '.join(unknown)}")
        return [{"templateid": self._templates[n]} for n in names]

    # ----------------------------------------------------------------- hosts
    def ensure_host(self, host, name, groups, templates=(), interfaces=(), macros=(), tags=()):
        """Create or update a managed host. Returns hostid."""
        if not NAME_RE.match(host):
            raise ApiError(f"invalid host name '{host}' (letters, digits, . _ -)")
        params = {
            "host": host,
            "name": name or host,
            "groups": [{"groupid": self.hostgroup(g)} for g in groups],
            "templates": self.template_ids(list(templates)),
            "macros": list(macros),
            "tags": [MANAGED_TAG] + list(tags),
            "status": 0,
        }
        found = self.api.call(
            "host.get",
            {"filter": {"host": [host]}, "output": ["hostid"], "selectInterfaces": "extend"},
        )
        if not found:
            params["interfaces"] = list(interfaces)
            hostid = self.api.call("host.create", params)["hostids"][0]
            log(f"created host {host}")
            return hostid
        hostid = found[0]["hostid"]
        self.api.call("host.update", dict(params, hostid=hostid))
        self._sync_interfaces(hostid, found[0]["interfaces"], interfaces)
        log(f"updated host {host}")
        return hostid

    def _sync_interfaces(self, hostid, existing, desired):
        """Update the main interface of each desired type in place; add missing ones.

        Interfaces are never deleted: items may depend on them.
        """
        for want in desired:
            have = next(
                (i for i in existing if int(i["type"]) == want["type"] and i["main"] == "1"),
                None,
            )
            if have is None:
                self.api.call("hostinterface.create", dict(want, hostid=hostid))
                continue
            fields = ("useip", "ip", "dns", "port")
            changed = any(str(have[k]) != str(want[k]) for k in fields)
            have_details = have.get("details") or {}
            if any(str(have_details.get(k)) != str(v) for k, v in (want.get("details") or {}).items()):
                changed = True
            if changed:
                upd = {k: want[k] for k in fields}
                upd["interfaceid"] = have["interfaceid"]
                if want.get("details"):
                    upd["details"] = want["details"]
                self.api.call("hostinterface.update", upd)

    def disable_stale(self, wanted_hosts):
        managed = self.api.call(
            "host.get",
            {"tags": [dict(MANAGED_TAG, operator=1)], "output": ["hostid", "host", "status"]},
        )
        for h in managed:
            if h["host"] not in wanted_hosts and h["status"] == "0":
                self.api.call("host.update", {"hostid": h["hostid"], "status": 1})
                log(f"disabled host {h['host']} (no longer in values)")


# --------------------------------------------------------------------------- #
# Interfaces and macros
# --------------------------------------------------------------------------- #
def agent_if(dns=None, ip=None, port="10050"):
    return {"type": IF_AGENT, "main": 1, "useip": 1 if ip else 0, "ip": ip or "", "dns": dns or "", "port": str(port)}


def snmp_if(ip, port="161"):
    return {
        "type": IF_SNMP,
        "main": 1,
        "useip": 1,
        "ip": ip,
        "dns": "",
        "port": str(port),
        "details": {"version": "2", "bulk": "1", "community": "{$SNMP_COMMUNITY}"},
    }


def macro(name, value, secret_value=False):
    return {"macro": name, "value": str(value), "type": MACRO_SECRET if secret_value else MACRO_TEXT}


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
def ensure_node_template(zbx, cfg):
    api = zbx.api
    name = cfg["templateName"]
    params = {
        "host": name,
        "name": name,
        "description": "Managed by zabbix-stack. Linux node monitored by agent 2 running in a container.",
        "groups": [{"groupid": zbx.templategroup(cfg["templateGroup"])}],
        "templates": zbx.template_ids(["Linux by Zabbix agent active"]),
        "macros": [
            # The agent sees the host filesystem under /hostfs/root.
            macro("{$VFS.FS.FSNAME.MATCHES}", r"^/hostfs/root(/.*)?$"),
            macro(
                "{$VFS.FS.FSNAME.NOT_MATCHES}",
                r"^/hostfs/root/(var/lib/(kubelet|containerd|docker)|run|proc|sys|dev)(/.*)?$",
            ),
            macro(
                "{$NET.IF.IFNAME.NOT_MATCHES}",
                r"(^Software Loopback Interface|^NULL[0-9.]*$|^[Ll]o[0-9.]*$|^[Ss]ystem$|^Nu[0-9.]*$"
                r"|^veth[0-9A-z]+$|docker[0-9]+|br-[a-z0-9]{12}|^cali[0-9a-f]+$|^tunl[0-9]+$"
                r"|^vxlan\.calico$|^kube-ipvs0$|^nodelocaldns$)",
            ),
            macro("{$NODE.TEMP.WARN}", cfg["tempWarnCelsius"]),
            macro("{$NODE.TEMP.HIGH}", cfg["tempHighCelsius"]),
        ],
    }
    found = api.call("template.get", {"filter": {"host": [name]}, "output": ["templateid"]})
    if found:
        tid = found[0]["templateid"]
        params.pop("host")
        api.call("template.update", dict(params, templateid=tid))
    else:
        tid = api.call("template.create", params)["templateids"][0]
        log(f"created template {name}")
    zbx._templates[name] = tid

    key = f"vfs.file.contents[{cfg['thermalZonePath']}]"
    item = {
        "name": "CPU temperature",
        "key_": key,
        "type": ITEM_ZABBIX_ACTIVE,
        "value_type": VALUE_FLOAT,
        "delay": "1m",
        "units": "°C",
        "history": "14d",
        "description": "SoC/CPU temperature from the kernel thermal zone (millidegrees).",
        "preprocessing": [{"type": 1, "params": "0.001", "error_handler": 0, "error_handler_params": ""}],
        "tags": [{"tag": "component", "value": "temperature"}],
    }
    items = api.call(
        "item.get", {"templateids": [tid], "filter": {"name": ["CPU temperature"]}, "output": ["itemid", "key_"]}
    )
    if items:
        api.call("item.update", dict(item, itemid=items[0]["itemid"]))
    else:
        api.call("item.create", dict(item, hostid=tid))

    expr = f"last(/{name}/{key})"
    high = ensure_trigger(
        api, tid, "Node {HOST.NAME}: CPU temperature is critical", f"{expr}>{{$NODE.TEMP.HIGH}}", HIGH
    )
    ensure_trigger(
        api,
        tid,
        "Node {HOST.NAME}: CPU temperature is high",
        f"{expr}>{{$NODE.TEMP.WARN}}",
        WARNING,
        depends_on=high,
    )
    return tid


def ensure_trigger(api, hostid, description, expression, priority, depends_on=None):
    params = {"description": description, "expression": expression, "priority": priority, "manual_close": 0}
    if depends_on:
        params["dependencies"] = [{"triggerid": depends_on}]
    found = api.call(
        "trigger.get",
        {"hostids": [hostid], "filter": {"description": [description]}, "output": ["triggerid"], "inherited": False},
    )
    if found:
        tid = found[0]["triggerid"]
        api.call("trigger.update", dict(params, triggerid=tid))
        return tid
    return api.call("trigger.create", params)["triggerids"][0]


def ensure_autoregistration(zbx, cfg, template_id):
    api = zbx.api
    params = {
        "name": cfg["actionName"],
        "eventsource": 2,
        "status": 0,
        "filter": {
            "evaltype": 0,
            # 24 = host metadata, 2 = contains
            "conditions": [{"conditiontype": 24, "operator": 2, "value": cfg["metadata"]}],
        },
        "operations": [
            {"operationtype": 2},  # add host
            {"operationtype": 4, "opgroup": [{"groupid": zbx.hostgroup(cfg["group"])}]},
            {"operationtype": 6, "optemplate": [{"templateid": template_id}]},
        ],
    }
    found = api.call("action.get", {"filter": {"name": [cfg["actionName"]]}, "output": ["actionid"]})
    if found:
        params.pop("eventsource")
        api.call("action.update", dict(params, actionid=found[0]["actionid"]))
    else:
        api.call("action.create", params)
        log(f"created action {cfg['actionName']}")


def fix_zabbix_server_host(zbx):
    """The default 'Zabbix server' host expects an agent on 127.0.0.1; there is none."""
    api = zbx.api
    found = api.call(
        "host.get",
        {"filter": {"host": ["Zabbix server"]}, "output": ["hostid"], "selectParentTemplates": ["templateid", "host"]},
    )
    if not found:
        return
    linked = [t for t in found[0]["parentTemplates"] if t["host"] == "Linux by Zabbix agent"]
    if linked:
        api.call(
            "host.update",
            {"hostid": found[0]["hostid"], "templates_clear": [{"templateid": t["templateid"]} for t in linked]},
        )
        log("unlinked 'Linux by Zabbix agent' from 'Zabbix server'")


# Templates that read custom resources from the Kubernetes API, linked to
# k8s-cluster-state, whose {$KUBE.API.URL} and {$KUBE.API.TOKEN} they use.
# Flux dropped its ready-condition metric in 2.x and Longhorn's metrics are
# split across its manager pods, so the API is the one complete source.
# The script returns {check: [names]}; each check becomes a count, a name list
# and a trigger (key, trigger name, minutes it must persist, priority).
CRD_JS = r"""
var p = JSON.parse(value), base = p.api_url.replace(/\/$/, '') + '/apis/';
function list(path) {
    var req = new HttpRequest();
    req.addHeader('Authorization: Bearer ' + p.api_token);
    var body = req.get(base + path);
    if (req.getStatus() === 404) return [];  // CRD not installed
    if (req.getStatus() !== 200) throw 'GET ' + path + ': ' + req.getStatus() + ' ' + body;
    return JSON.parse(body).items;
}
function ready(o) {
    var c = ((o.status || {}).conditions || []).filter(function (c) { return c.type === 'Ready'; });
    return c.length > 0 && c[0].status === 'True';
}
"""
CRD_TEMPLATES = {
    "flux": {
        "name": "Flux by Kubernetes API",
        "script": CRD_JS + r"""
var out = {not_ready: []};
['kustomize.toolkit.fluxcd.io/v1/kustomizations', 'helm.toolkit.fluxcd.io/v2/helmreleases',
 'source.toolkit.fluxcd.io/v1/ocirepositories', 'source.toolkit.fluxcd.io/v1/gitrepositories',
 'source.toolkit.fluxcd.io/v1/helmrepositories'].forEach(function (path) {
    list(path).forEach(function (o) {
        if (!(o.spec || {}).suspend && !ready(o))
            out.not_ready.push(path.split('/').pop() + ' ' + o.metadata.namespace + '/' + o.metadata.name);
    });
});
return JSON.stringify(out);
""",
        # A failed signature check or upgrade leaves the object not Ready while
        # the old version keeps running: worth a look, not an outage.
        "checks": [("not_ready", "Flux objects not ready", 15, AVERAGE)],
    },
    "longhorn": {
        "name": "Longhorn by Kubernetes API",
        "script": CRD_JS + r"""
var out = {degraded: [], faulted: [], nodes_not_ready: []};
list('longhorn.io/v1beta2/volumes').forEach(function (v) {
    var s = v.status || {}, k = s.kubernetesStatus || {};
    var name = k.pvcName ? k.namespace + '/' + k.pvcName : v.metadata.name;
    if (s.robustness === 'degraded') out.degraded.push(name);
    if (s.robustness === 'faulted') out.faulted.push(name);
});
list('longhorn.io/v1beta2/nodes').forEach(function (n) {
    if (!ready(n)) out.nodes_not_ready.push(n.metadata.name);
});
return JSON.stringify(out);
""",
        "checks": [
            # Degraded = running on fewer replicas; normal for a while after a node drain.
            ("degraded", "Longhorn volumes degraded", 30, AVERAGE),
            ("faulted", "Longhorn volumes faulted", 0, HIGH),
            ("nodes_not_ready", "Longhorn nodes not ready", 10, AVERAGE),
        ],
    },
}


def ensure_item(api, hostid, item):
    found = api.call("item.get", {"hostids": [hostid], "filter": {"key_": [item["key_"]]}, "output": ["itemid"]})
    if found:
        api.call("item.update", dict(item, itemid=found[0]["itemid"]))
        return found[0]["itemid"]
    return api.call("item.create", dict(item, hostid=hostid))["itemids"][0]


def ensure_crd_template(zbx, key, group):
    api, spec = zbx.api, CRD_TEMPLATES[key]
    name = spec["name"]
    params = {
        "host": name,
        "name": name,
        "description": "Managed by zabbix-stack. Reads custom resources through {$KUBE.API.URL}.",
        "groups": [{"groupid": zbx.templategroup(group)}],
    }
    found = api.call("template.get", {"filter": {"host": [name]}, "output": ["templateid"]})
    if found:
        tid = found[0]["templateid"]
        params.pop("host")
        api.call("template.update", dict(params, templateid=tid))
    else:
        tid = api.call("template.create", params)["templateids"][0]
        log(f"created template {name}")
    zbx._templates[name] = tid

    master = ensure_item(api, tid, {
        "name": f"{name}: raw", "key_": f"{key}.crd.raw", "type": ITEM_SCRIPT, "value_type": VALUE_TEXT,
        "delay": "1m", "history": "0", "timeout": "30s", "params": spec["script"],
        "parameters": [{"name": "api_url", "value": "{$KUBE.API.URL}"},
                       {"name": "api_token", "value": "{$KUBE.API.TOKEN}"}],
    })
    for check, title, minutes, priority in spec["checks"]:
        def dependent(item_key, item_name, value_type, js):
            return ensure_item(api, tid, {
                "name": item_name, "key_": item_key, "type": ITEM_DEPENDENT, "master_itemid": master,
                "value_type": value_type, "history": "7d",
                "preprocessing": [{"type": 21, "params": js, "error_handler": 0, "error_handler_params": ""}],
                "tags": [{"tag": "component", "value": key}],
            })
        count_key, names_key = f"{key}.{check}.count", f"{key}.{check}"
        dependent(count_key, f"{title}: count", VALUE_UNSIGNED, f"return JSON.parse(value).{check}.length;")
        dependent(names_key, title, VALUE_TEXT, f"return JSON.parse(value).{check}.join(', ');")
        # The names item is in the expression only so the problem name can show them.
        held = f"min(/{name}/{count_key},{minutes}m)" if minutes else f"last(/{name}/{count_key})"
        ensure_trigger(api, tid, f"{title}: {{ITEM.LASTVALUE2}}",
                       f'{held}>0 and last(/{name}/{names_key})<>""', priority)
    # Without this an API or RBAC failure would look like "all healthy".
    first = f"{key}.{spec['checks'][0][0]}.count"
    ensure_trigger(api, tid, f"{name}: no data for 30m", f"nodata(/{name}/{first},30m)=1", WARNING)


def configure_kubernetes(zbx, cfg, wanted):
    token = wait_for_file(os.environ.get("K8S_TOKEN_FILE", "/k8s-token/token"))
    extra = [macro(k, v) for k, v in (cfg.get("extraMacros") or {}).items()]
    base = [macro("{$KUBE.API.URL}", cfg["apiUrl"]), macro("{$KUBE.API.TOKEN}", token, True)]
    crd = [CRD_TEMPLATES[k]["name"] for k in CRD_TEMPLATES if cfg.get(k)]
    for k in CRD_TEMPLATES:
        if cfg.get(k):
            ensure_crd_template(zbx, k, cfg["templateGroup"])
    hosts = [
        (
            "k8s-cluster-state",
            "Kubernetes cluster state",
            ["Kubernetes cluster state by HTTP"] + crd,
            [macro("{$KUBE.STATE.ENDPOINT.NAME}", cfg["stateEndpoint"])],
        ),
        (
            "k8s-nodes",
            "Kubernetes nodes",
            ["Kubernetes nodes by HTTP"],
            [macro("{$KUBE.NODES.ENDPOINT.NAME}", cfg["nodesEndpoint"])],
        ),
    ]
    for host, name, templates, own in hosts:
        zbx.ensure_host(host, name, [cfg["group"]], templates, macros=base + own + extra,
                        tags=[{"tag": "role", "value": "kubernetes"}])
        wanted.add(host)


def configure_snmp_devices(zbx, devices, group, community, role, wanted):
    for d in devices or []:
        snmp = d.get("snmp", True)
        if snmp and not community:
            raise SystemExit(f"device {d['name']} uses SNMP but the community secret key is empty")
        templates = d.get("templates") or (["Network Generic Device by SNMP"] if snmp else ["ICMP Ping"])
        iface = snmp_if(d["address"], d.get("port", 161)) if snmp else agent_if(ip=d["address"])
        macros = [macro("{$SNMP_COMMUNITY}", community, True)] if snmp else []
        zbx.ensure_host(
            d["name"],
            d.get("visibleName", d["name"]),
            [group] + list(d.get("extraGroups", [])),
            templates,
            interfaces=[iface],
            macros=macros,
            tags=[{"tag": "role", "value": role}],
        )
        wanted.add(d["name"])


def configure_databases(zbx, cfg, wanted):
    if not cfg.get("probeEnabled"):
        if cfg.get("selfMonitor") or cfg.get("instances"):
            log("probe agent disabled; skipping database hosts")
        return
    targets = []
    if cfg.get("selfMonitor"):
        targets.append(
            {
                "name": "pg-zabbix",
                "visibleName": "PostgreSQL (zabbix)",
                "connString": cfg["selfConnString"],
                "database": secret("db-name"),
                "user": secret("db-user"),
                "password": secret("db-password"),
            }
        )
    for i in cfg.get("instances") or []:
        targets.append(dict(i, password=secret(i["passwordKey"])))
    for t in targets:
        zbx.ensure_host(
            t["name"],
            t.get("visibleName", t["name"]),
            [cfg["group"]],
            ["PostgreSQL by Zabbix agent 2"],
            interfaces=[agent_if(dns=cfg["probeHost"])],
            macros=[
                macro("{$PG.CONNSTRING.AGENT2}", t["connString"]),
                macro("{$PG.DATABASE}", t.get("database", "postgres")),
                macro("{$PG.USER}", t["user"]),
                macro("{$PG.PASSWORD}", t["password"], True),
            ],
            tags=[{"tag": "role", "value": "database"}],
        )
        wanted.add(t["name"])


def configure_services(zbx, cfg, wanted):
    api = zbx.api
    for s in cfg.get("endpoints") or []:
        name = s["name"]
        if not NAME_RE.match(name):
            raise SystemExit(f"service name '{name}' must match {NAME_RE.pattern}")
        host = f"svc-{name}"
        url = urllib.parse.urlsplit(s["url"])
        templates, interfaces, macros = [], [], []
        if s.get("certificate"):
            if url.scheme != "https":
                raise SystemExit(f"service {name}: certificate check needs an https URL")
            if not cfg.get("probeEnabled"):
                raise SystemExit(f"service {name}: certificate check needs probeAgent.enabled")
            templates = ["Website certificate by Zabbix agent 2"]
            interfaces = [agent_if(dns=cfg["probeHost"])]
            macros = [
                macro("{$CERT.WEBSITE.HOSTNAME}", url.hostname),
                macro("{$CERT.WEBSITE.PORT}", url.port or 443),
            ]
        hostid = zbx.ensure_host(
            host, s.get("visibleName", name), [cfg["group"]], templates, interfaces, macros,
            tags=[{"tag": "role", "value": "service"}],
        )
        wanted.add(host)

        scenario = {
            "name": name,
            "delay": s.get("interval", cfg.get("checkInterval", "1m")),
            "retries": 2,
            "verify_peer": 1 if s.get("verifyTls", True) else 0,
            "verify_host": 1 if s.get("verifyTls", True) else 0,
            "steps": [
                {
                    "name": "request",
                    "no": 1,
                    "url": s["url"],
                    "status_codes": str(s.get("expectStatus", "200")),
                    "follow_redirects": 1,
                    "timeout": "15s",
                }
            ],
        }
        found = api.call("httptest.get", {"hostids": [hostid], "filter": {"name": [name]}, "output": ["httptestid"]})
        if found:
            api.call("httptest.update", dict(scenario, httptestid=found[0]["httptestid"]))
        else:
            api.call("httptest.create", dict(scenario, hostid=hostid))
        ensure_trigger(
            api,
            hostid,
            f"Service {name} is down",
            f"last(/{host}/web.test.fail[{name}])<>0",
            HIGH,
        )


def ensure_global_macro(api, name, value):
    found = api.call("usermacro.get", {"globalmacro": True, "filter": {"macro": name}, "output": ["globalmacroid", "value"]})
    if not found:
        api.call("usermacro.createglobal", {"macro": name, "value": value})
    elif found[0]["value"] != value:
        api.call("usermacro.updateglobal", {"globalmacroid": found[0]["globalmacroid"], "value": value})


def configure_discord(zbx, cfg):
    """Built-in Discord media type on Admin, and an action for problems at or above minSeverity.

    Disabling it in values disables the action; the media stays.
    """
    api = zbx.api
    found = api.call("action.get", {"filter": {"name": [DISCORD_ACTION]}, "output": ["actionid", "status"]})
    if not cfg.get("enabled"):
        if found and found[0]["status"] == "0":
            api.call("action.update", {"actionid": found[0]["actionid"], "status": 1})
            log(f"disabled action {DISCORD_ACTION}")
        return

    # The media type builds its problem links from this macro and fails without it.
    ensure_global_macro(api, "{$ZABBIX.URL}", cfg["zabbixUrl"])
    mt = api.call("mediatype.get", {"filter": {"name": ["Discord"]}, "output": ["mediatypeid", "status"]})
    if not mt:
        raise ApiError("media type Discord not found")
    mtid = mt[0]["mediatypeid"]
    if mt[0]["status"] != "0":
        api.call("mediatype.update", {"mediatypeid": mtid, "status": 0})
        log("enabled media type Discord")

    # user.update replaces all medias: keep Admin's others as they are.
    user = api.call(
        "user.get",
        {"filter": {"username": "Admin"}, "output": ["userid"],
         "selectMedias": ["mediatypeid", "sendto", "active", "severity", "period"]},
    )[0]
    fields = ("mediatypeid", "sendto", "active", "severity", "period")
    medias = [{k: m[k] for k in fields} for m in user["medias"] if m["mediatypeid"] != mtid]
    medias.append({"mediatypeid": mtid, "sendto": secret(cfg["webhookKey"]), "active": 0, "severity": 63,
                   "period": "1-7,00:00-24:00"})
    api.call("user.update", {"userid": user["userid"], "medias": medias})

    params = {
        "name": DISCORD_ACTION,
        "eventsource": 0,
        "status": 0,
        "esc_period": "1h",
        "filter": {
            "evaltype": 0,
            "conditions": [
                # 4 = trigger severity, 5 = greater or equal
                {"conditiontype": 4, "operator": 5, "value": str(cfg["minSeverity"])},
                # 16 = problem is suppressed (maintenance), 11 = no
                {"conditiontype": 16, "operator": 11},
            ],
        },
        "operations": [
            {"operationtype": 0, "opmessage": {"default_msg": 1, "mediatypeid": mtid},
             "opmessage_usr": [{"userid": user["userid"]}]},
        ],
        # 11 = notify all involved. No update operations: every triage message would post.
        "recovery_operations": [{"operationtype": 11, "opmessage": {"default_msg": 1}}],
    }
    if found:
        params.pop("eventsource")
        api.call("action.update", dict(params, actionid=found[0]["actionid"]))
    else:
        api.call("action.create", params)
        log(f"created action {DISCORD_ACTION}")


# --------------------------------------------------------------------------- #
def main(path):
    with open(path, encoding="utf-8") as f:
        state = json.load(f)

    api = Api(state["apiUrl"])
    wait_for_api(api)
    login(api, state.get("rotateAdminPassword", True))
    zbx = Zabbix(api)

    for g in state.get("hostGroups") or []:
        zbx.hostgroup(g)

    fix_zabbix_server_host(zbx)

    nodes = state["nodes"]
    tid = ensure_node_template(zbx, nodes)
    ensure_autoregistration(zbx, nodes, tid)

    wanted = set()
    if state["kubernetes"].get("enabled"):
        configure_kubernetes(zbx, state["kubernetes"], wanted)

    community = secret(state["network"].get("snmpCommunityKey", "snmp-community"), required=False)
    configure_snmp_devices(zbx, state["network"].get("devices"), state["network"]["group"], community, "network", wanted)
    configure_snmp_devices(zbx, state["rack"].get("devices"), state["rack"]["group"], community, "rack", wanted)
    configure_databases(zbx, state["databases"], wanted)
    configure_services(zbx, state["services"], wanted)
    configure_discord(zbx, state["discord"])

    zbx.disable_stale(wanted)
    log(f"done: {len(wanted)} managed hosts")


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except ApiError as exc:
        log(f"API error: {exc}")
        sys.exit(1)
