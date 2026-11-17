"""SQLite 存储：衣橱单品、搭配、客户档案、设置都放在 data/closet.db 一个文件里。

对 app.py 保持原来的用法：db_load() 读出一个字典，改完 db_save(db) 存回去。
存回去时只写有变化的行，整个过程在一个事务里，写到一半断电不会留下半截数据。
老的 data/db.json 第一次启动时自动导入，原文件改名为 db.json.migrated 留作备份。
"""
import json, sqlite3, threading
from pathlib import Path

# 每张表：表名 -> 单独拿出来做成列的字段（方便以后按列查询、排序）；其余字段放在 data 列的 JSON 里
TABLES = {
    "items": ("category", "created"),
    "looks": ("created",),
    "profiles": ("name", "phone", "company", "stage", "created", "updated"),
}
META_TABLE = "meta"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, category TEXT, created REAL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS items_category ON items(category);
CREATE TABLE IF NOT EXISTS looks(id TEXT PRIMARY KEY, created REAL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, name TEXT, phone TEXT, company TEXT, stage TEXT,
                                    created REAL, updated REAL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS profiles_name ON profiles(name);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tl = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        c = getattr(self._tl, "c", None)
        if c is None:
            c = self._tl.c = sqlite3.connect(self.path, timeout=15)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        return c

    # ---------------------------------------------------------- 衣橱 / 搭配 / 客户
    def load(self):
        c = self._conn()
        db = {t: {} for t in TABLES}
        for t in TABLES:
            for rid, data in c.execute(f"SELECT id, data FROM {t}"):
                db[t][rid] = json.loads(data)
        for k, v in c.execute(f"SELECT k, v FROM {META_TABLE}"):
            db[k] = json.loads(v)
        return db

    def save(self, db):
        c = self._conn()
        with c:  # 一个事务
            for t, cols in TABLES.items():
                rows = db.get(t, {})
                old = {rid: data for rid, data in c.execute(f"SELECT id, data FROM {t}")}
                for rid, doc in rows.items():
                    js = json.dumps(doc, ensure_ascii=False, sort_keys=True)
                    if old.get(rid) == js:
                        continue
                    vals = [doc.get(k) for k in cols]
                    c.execute(f"INSERT OR REPLACE INTO {t}(id, {', '.join(cols)}, data) "
                              f"VALUES(?, {', '.join('?' * len(cols))}, ?)", [rid, *vals, js])
                gone = [rid for rid in old if rid not in rows]
                if gone:
                    c.executemany(f"DELETE FROM {t} WHERE id=?", [(r,) for r in gone])
            meta = {k: v for k, v in db.items() if k not in TABLES}
            c.execute(f"DELETE FROM {META_TABLE}")
            c.executemany(f"INSERT INTO {META_TABLE}(k, v) VALUES(?, ?)",
                          [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])

    def is_empty(self):
        c = self._conn()
        return not any(c.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in TABLES)

    def import_json(self, jpath: Path):
        """把老的 db.json 导进来。成功后原文件改名留作备份；没有或已经导过就什么都不做。"""
        jpath = Path(jpath)
        if not jpath.exists() or not self.is_empty():
            return False
        db = json.loads(jpath.read_text(encoding="utf-8"))
        db.setdefault("items", {}), db.setdefault("looks", {}), db.setdefault("profiles", {})
        self.save(db)
        jpath.replace(jpath.with_name(jpath.name + ".migrated"))
        return True

    # ---------------------------------------------------------- 设置
    def settings(self):
        return {k: v for k, v in self._conn().execute("SELECT k, v FROM settings")}

    def set_settings(self, put=None, delete=()):
        c = self._conn()
        with c:
            for k, v in (put or {}).items():
                c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, v))
            for k in delete:
                c.execute("DELETE FROM settings WHERE k=?", (k,))
