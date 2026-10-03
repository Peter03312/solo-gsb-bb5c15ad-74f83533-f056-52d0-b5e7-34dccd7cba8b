"""POST /preview：草稿相对基线版本的投递变化预览（只读、同一次读取、整次失败语义）。"""
import os
import tempfile

# 必须在导入 app 之前设置，config 在导入时读取
os.environ["ROUTER_DB"] = os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db")

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM draft_rules;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


# 与 test_api 相同的典型规则：精确优先、域兜底、多分支、别名链、拒收
RULES = [
    {
        "match_type": "exact",
        "match_key": "boss@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "alias", "value": "assistant@corp.example.com"},
            {"kind": "mailbox", "value": "boss-backup@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "assistant@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
            {"kind": "alias", "value": "team@corp.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "team@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "alice@mail.example.com"},
            {"kind": "mailbox", "value": "bob@mail.example.com"},
        ],
    },
    {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "reject",
        "targets": [],
    },
    {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "catchall@mail.example.com"}],
    },
]


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def setup_published(client, rules=RULES):
    assert put_rules(client, 0, rules).status_code == 200
    resp = publish(client, 1)
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


def preview(client, baseline_version, revision):
    return client.post("/preview", json={"baseline_version": baseline_version, "revision": revision})


def replace_rule(rules, match_key, new_rule):
    return [r if r["match_key"] != match_key else new_rule for r in rules]


# ---------- 无变化 / 目标顺序不计 ----------

def test_preview_no_changes_when_draft_matches_baseline(client):
    version = setup_published(client)
    resp = preview(client, version, 1)
    assert resp.status_code == 200
    assert resp.json() == {
        "baseline_version": version,
        "revision": 1,
        "changed_addresses": [],
        "changed_domain_fallbacks": [],
    }


def test_preview_ignores_target_order(client):
    version = setup_published(client)
    # team 的两个 mailbox 目标调换顺序：终点与路径集合不变 → 不算差异
    rules = replace_rule(RULES, "team@corp.example.com", {
        "match_type": "exact",
        "match_key": "team@corp.example.com",
        "action": "route",
        "targets": [
            {"kind": "mailbox", "value": "bob@mail.example.com"},
            {"kind": "mailbox", "value": "alice@mail.example.com"},
        ],
    })
    assert put_rules(client, 1, rules).status_code == 200
    body = preview(client, version, 2).json()
    assert body["changed_addresses"] == []
    assert body["changed_domain_fallbacks"] == []


# ---------- 精确地址变化：两侧完整结果 ----------

def test_preview_exact_change_lists_address_with_both_results(client):
    version = setup_published(client)
    # blocked 从拒收改为投递
    rules = replace_rule(RULES, "blocked@corp.example.com", {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "blocked@mail.example.com"}],
    })
    assert put_rules(client, 1, rules).status_code == 200
    resp = preview(client, version, 2)
    assert resp.status_code == 200
    body = resp.json()
    assert body["baseline_version"] == version
    assert body["revision"] == 2

    changed = {c["address"]: c for c in body["changed_addresses"]}
    assert set(changed) == {"blocked@corp.example.com"}
    entry = changed["blocked@corp.example.com"]
    assert entry["baseline"]["status"] == "failed"
    assert entry["baseline"]["error"]["error"] == "rejected"
    assert entry["baseline"]["error"]["chain"] == ["blocked@corp.example.com"]
    assert entry["draft"]["status"] == "ok"
    assert entry["draft"]["endpoints"] == [
        {"mailbox": "blocked@mail.example.com",
         "paths": [["blocked@corp.example.com", "blocked@mail.example.com"]]}
    ]
    assert body["changed_domain_fallbacks"] == []


def test_preview_alias_downstream_change_traced_to_entries(client):
    version = setup_published(client)
    # team 改目标：引用 team 的 assistant、引用 assistant 的 boss 都应被追溯列出
    rules = replace_rule(RULES, "team@corp.example.com", {
        "match_type": "exact",
        "match_key": "team@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "carol@mail.example.com"}],
    })
    assert put_rules(client, 1, rules).status_code == 200
    body = preview(client, version, 2).json()
    changed = {c["address"]: c for c in body["changed_addresses"]}
    assert set(changed) == {
        "team@corp.example.com",
        "assistant@corp.example.com",
        "boss@corp.example.com",
    }
    # 入口侧给出完整新结果：boss 的终点变为 alice（直发）+ carol + boss-backup
    boss = changed["boss@corp.example.com"]
    assert {e["mailbox"] for e in boss["baseline"]["endpoints"]} == {
        "alice@mail.example.com", "bob@mail.example.com", "boss-backup@mail.example.com",
    }
    assert {e["mailbox"] for e in boss["draft"]["endpoints"]} == {
        "alice@mail.example.com", "carol@mail.example.com", "boss-backup@mail.example.com",
    }
    # 未受影响的精确地址不列出
    assert "blocked@corp.example.com" not in changed


# ---------- 草稿未通过发布校验：预览仍展示失败结果 ----------

def test_preview_unpublishable_draft_cycle_still_shown(client):
    version = setup_published(client)
    cyclic = RULES + [
        {"match_type": "exact", "match_key": "a@corp.example.com", "action": "route",
         "targets": [{"kind": "alias", "value": "b@corp.example.com"}]},
        {"match_type": "exact", "match_key": "b@corp.example.com", "action": "route",
         "targets": [{"kind": "alias", "value": "a@corp.example.com"}]},
    ]
    assert put_rules(client, 1, cyclic).status_code == 200
    assert publish(client, 2).status_code == 422  # 草稿未通过发布校验

    resp = preview(client, version, 2)
    assert resp.status_code == 200
    body = resp.json()
    changed = {c["address"]: c for c in body["changed_addresses"]}
    assert set(changed) == {"a@corp.example.com", "b@corp.example.com"}
    for addr in ("a@corp.example.com", "b@corp.example.com"):
        assert changed[addr]["baseline"]["status"] == "ok"  # 基线里走域兜底
        assert changed[addr]["draft"]["status"] == "failed"
        assert changed[addr]["draft"]["error"]["error"] == "cycle"
    assert body["changed_domain_fallbacks"] == []


def test_preview_unpublishable_draft_no_route_still_shown(client):
    version = setup_published(client)
    rules = RULES + [{
        "match_type": "exact", "match_key": "ghost-entry@corp.example.com", "action": "route",
        "targets": [{"kind": "alias", "value": "ghost@other.example.com"}],  # 目标域无任何规则
    }]
    assert put_rules(client, 1, rules).status_code == 200
    assert publish(client, 2).status_code == 422

    body = preview(client, version, 2).json()
    changed = {c["address"]: c for c in body["changed_addresses"]}
    # ghost@other.example.com 两侧都是 no_route，结果相同，不列出
    assert set(changed) == {"ghost-entry@corp.example.com"}
    entry = changed["ghost-entry@corp.example.com"]
    assert entry["baseline"]["status"] == "ok"
    assert entry["draft"]["status"] == "failed"
    assert entry["draft"]["error"]["error"] == "no_route"
    assert entry["draft"]["error"]["address"] == "ghost@other.example.com"


# ---------- 域兜底类别 ----------

def test_preview_domain_fallback_change(client):
    version = setup_published(client)
    rules = replace_rule(RULES, "corp.example.com", {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "new-catchall@mail.example.com"}],
    })
    assert put_rules(client, 1, rules).status_code == 200
    body = preview(client, version, 2).json()
    # 精确地址与别名目标都命中精确规则，不受域兜底影响
    assert body["changed_addresses"] == []
    fallbacks = body["changed_domain_fallbacks"]
    assert len(fallbacks) == 1
    fb = fallbacks[0]
    assert fb["domain"] == "corp.example.com"
    assert fb["sample_address"] == "preview-probe@corp.example.com"
    assert fb["baseline"]["status"] == "ok"
    assert fb["baseline"]["endpoints"][0]["mailbox"] == "catchall@mail.example.com"
    assert fb["draft"]["status"] == "ok"
    assert fb["draft"]["endpoints"][0]["mailbox"] == "new-catchall@mail.example.com"


def test_preview_domain_rule_removed(client):
    version = setup_published(client)
    rules = [r for r in RULES if r["match_type"] != "domain"]
    assert put_rules(client, 1, rules).status_code == 200
    body = preview(client, version, 2).json()
    assert body["changed_addresses"] == []
    fallbacks = body["changed_domain_fallbacks"]
    assert len(fallbacks) == 1
    fb = fallbacks[0]
    assert fb["domain"] == "corp.example.com"
    assert fb["baseline"]["status"] == "ok"
    assert fb["draft"]["status"] == "failed"
    assert fb["draft"]["error"]["error"] == "no_route"


def test_preview_domain_rule_added(client):
    # 基线无域规则，草稿新增域兜底
    exact_only = [r for r in RULES if r["match_type"] != "domain"]
    version = setup_published(client, rules=exact_only)
    assert put_rules(client, 1, RULES).status_code == 200
    body = preview(client, version, 2).json()
    fallbacks = body["changed_domain_fallbacks"]
    assert len(fallbacks) == 1
    fb = fallbacks[0]
    assert fb["baseline"]["status"] == "failed"
    assert fb["baseline"]["error"]["error"] == "no_route"
    assert fb["draft"]["status"] == "ok"
    assert fb["draft"]["endpoints"][0]["mailbox"] == "catchall@mail.example.com"


def test_preview_sample_address_avoids_exact_and_alias(client):
    # 两侧具体地址占用 preview-probe 与 preview-probe-1 → 示例地址顺延
    extra = [
        {"match_type": "exact", "match_key": "preview-probe@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "probe@mail.example.com"}]},
        {"match_type": "exact", "match_key": "sink@corp.example.com", "action": "route",
         "targets": [{"kind": "alias", "value": "preview-probe-1@corp.example.com"}]},
        {"match_type": "exact", "match_key": "preview-probe-1@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "probe1@mail.example.com"}]},
    ]
    rules = RULES + extra
    version = setup_published(client, rules=rules)
    changed_rules = replace_rule(rules, "corp.example.com", {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "new-catchall@mail.example.com"}],
    })
    assert put_rules(client, 1, changed_rules).status_code == 200
    body = preview(client, version, 2).json()
    fb = body["changed_domain_fallbacks"][0]
    assert fb["sample_address"] == "preview-probe-2@corp.example.com"


# ---------- 错误契约与只读性 ----------

def test_preview_stale_revision_conflict(client):
    version = setup_published(client)
    resp = preview(client, version, 0)  # 草稿已是 revision 1
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 1


def test_preview_unknown_baseline_version_404(client):
    version = setup_published(client)
    resp = preview(client, 99, 1)
    assert resp.status_code == 404
    assert resp.json()["detail"] == {"error": "version_not_found", "version": 99}


def test_preview_is_readonly(client):
    version = setup_published(client)
    rules = replace_rule(RULES, "blocked@corp.example.com", {
        "match_type": "exact",
        "match_key": "blocked@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "blocked@mail.example.com"}],
    })
    assert put_rules(client, 1, rules).status_code == 200
    assert preview(client, version, 2).status_code == 200
    # 草稿与历史版本均未被改动
    assert client.get("/draft").json()["revision"] == 2
    versions = client.get("/versions").json()["versions"]
    assert [v["version"] for v in versions] == [version]
    # 基线版本解析结果仍可复现
    resp = client.post("/resolve", json={"address": "blocked@corp.example.com", "version": version})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "rejected"
