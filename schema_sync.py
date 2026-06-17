"""
Schema Sync Service - 对比两个数据库的表结构差异，生成 ALTER 语句
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import (
    CreateTable,
    CreateIndex,
    MetaData,
    Table,
)


@dataclass
class ColumnInfo:
    name: str
    type_: str
    nullable: bool
    default: Optional[str]
    autoincrement: bool
    is_primary_key: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type_,
            "nullable": self.nullable,
            "default": self.default,
            "autoincrement": self.autoincrement,
            "is_primary_key": self.is_primary_key,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ColumnInfo":
        return cls(
            name=d["name"],
            type_=d["type"],
            nullable=d["nullable"],
            default=d["default"],
            autoincrement=d["autoincrement"],
            is_primary_key=d["is_primary_key"],
        )


@dataclass
class IndexInfo:
    name: str
    columns: List[str]
    unique: bool

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "columns": self.columns, "unique": self.unique}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "IndexInfo":
        return cls(name=d["name"], columns=d["columns"], unique=d["unique"])


@dataclass
class UniqueConstraintInfo:
    name: str
    columns: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "columns": self.columns}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UniqueConstraintInfo":
        return cls(name=d["name"], columns=d["columns"])


@dataclass
class ForeignKeyInfo:
    name: str
    constrained_columns: List[str]
    referred_table: str
    referred_columns: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "constrained_columns": self.constrained_columns,
            "referred_table": self.referred_table,
            "referred_columns": self.referred_columns,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ForeignKeyInfo":
        return cls(
            name=d["name"],
            constrained_columns=d["constrained_columns"],
            referred_table=d["referred_table"],
            referred_columns=d["referred_columns"],
        )


@dataclass
class TableSchema:
    name: str
    columns: Dict[str, ColumnInfo] = field(default_factory=dict)
    indexes: Dict[str, IndexInfo] = field(default_factory=dict)
    unique_constraints: Dict[str, UniqueConstraintInfo] = field(default_factory=dict)
    foreign_keys: Dict[str, ForeignKeyInfo] = field(default_factory=dict)
    primary_key_columns: List[str] = field(default_factory=list)


@dataclass
class DatabaseSchema:
    tables: Dict[str, TableSchema] = field(default_factory=dict)


class DatabaseInspector:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.inspector = inspect(engine)

    def inspect_schema(self, schema: Optional[str] = None) -> DatabaseSchema:
        db_schema = DatabaseSchema()
        table_names = self.inspector.get_table_names(schema=schema)

        for table_name in table_names:
            table_schema = self._inspect_table(table_name, schema)
            db_schema.tables[table_name] = table_schema

        return db_schema

    def _inspect_table(
        self, table_name: str, schema: Optional[str] = None
    ) -> TableSchema:
        table = TableSchema(name=table_name)

        pk_constraint = self.inspector.get_pk_constraint(table_name, schema=schema)
        pk_columns = pk_constraint.get("constrained_columns", [])
        table.primary_key_columns = list(pk_columns)

        for col in self.inspector.get_columns(table_name, schema=schema):
            default_val = col.get("default")
            if default_val is not None and hasattr(default_val, "arg"):
                default_val = str(default_val.arg) if not isinstance(default_val.arg, str) else default_val.arg

            col_info = ColumnInfo(
                name=col["name"],
                type_=str(col["type"]),
                nullable=col.get("nullable", True),
                default=default_val,
                autoincrement=col.get("autoincrement", False),
                is_primary_key=col["name"] in pk_columns,
            )
            table.columns[col_info.name] = col_info

        for idx in self.inspector.get_indexes(table_name, schema=schema):
            idx_info = IndexInfo(
                name=idx["name"],
                columns=idx["column_names"],
                unique=idx.get("unique", False),
            )
            table.indexes[idx_info.name] = idx_info

        for uc in self.inspector.get_unique_constraints(table_name, schema=schema):
            uc_info = UniqueConstraintInfo(
                name=uc.get("name", ""),
                columns=uc["column_names"],
            )
            if uc_info.name:
                table.unique_constraints[uc_info.name] = uc_info

        for fk in self.inspector.get_foreign_keys(table_name, schema=schema):
            fk_info = ForeignKeyInfo(
                name=fk.get("name", ""),
                constrained_columns=fk["constrained_columns"],
                referred_table=fk["referred_table"],
                referred_columns=fk["referred_columns"],
            )
            if fk_info.name:
                table.foreign_keys[fk_info.name] = fk_info

        return table


@dataclass
class ColumnDiff:
    table_name: str
    column_name: str
    change_type: str
    source_value: Any = None
    target_value: Any = None


@dataclass
class TableDiff:
    table_name: str
    change_type: str
    source_table: Optional[TableSchema] = None
    target_table: Optional[TableSchema] = None
    added_columns: List[ColumnInfo] = field(default_factory=list)
    dropped_columns: List[ColumnInfo] = field(default_factory=list)
    modified_columns: List[ColumnDiff] = field(default_factory=list)
    added_indexes: List[IndexInfo] = field(default_factory=list)
    dropped_indexes: List[IndexInfo] = field(default_factory=list)
    added_unique_constraints: List[UniqueConstraintInfo] = field(default_factory=list)
    dropped_unique_constraints: List[UniqueConstraintInfo] = field(default_factory=list)
    added_foreign_keys: List[ForeignKeyInfo] = field(default_factory=list)
    dropped_foreign_keys: List[ForeignKeyInfo] = field(default_factory=list)
    pk_changed: bool = False
    source_pk: List[str] = field(default_factory=list)
    target_pk: List[str] = field(default_factory=list)


@dataclass
class SchemaDiff:
    added_tables: List[TableSchema] = field(default_factory=list)
    dropped_tables: List[TableSchema] = field(default_factory=list)
    table_diffs: List[TableDiff] = field(default_factory=list)


class SchemaDiffer:
    def diff(self, source: DatabaseSchema, target: DatabaseSchema) -> SchemaDiff:
        result = SchemaDiff()

        source_tables = set(source.tables.keys())
        target_tables = set(target.tables.keys())

        for table_name in source_tables - target_tables:
            result.added_tables.append(source.tables[table_name])

        for table_name in target_tables - source_tables:
            result.dropped_tables.append(target.tables[table_name])

        for table_name in source_tables & target_tables:
            table_diff = self._diff_table(
                table_name, source.tables[table_name], target.tables[table_name]
            )
            if self._table_diff_has_changes(table_diff):
                result.table_diffs.append(table_diff)

        return result

    def _table_diff_has_changes(self, td: TableDiff) -> bool:
        return bool(
            td.added_columns
            or td.dropped_columns
            or td.modified_columns
            or td.added_indexes
            or td.dropped_indexes
            or td.added_unique_constraints
            or td.dropped_unique_constraints
            or td.added_foreign_keys
            or td.dropped_foreign_keys
            or td.pk_changed
        )

    def _diff_table(
        self, table_name: str, source: TableSchema, target: TableSchema
    ) -> TableDiff:
        td = TableDiff(
            table_name=table_name,
            change_type="modified",
            source_table=source,
            target_table=target,
        )

        source_cols = set(source.columns.keys())
        target_cols = set(target.columns.keys())

        for col_name in source_cols - target_cols:
            td.added_columns.append(source.columns[col_name])

        for col_name in target_cols - source_cols:
            td.dropped_columns.append(target.columns[col_name])

        for col_name in source_cols & target_cols:
            col_diff = self._diff_column(
                table_name, col_name, source.columns[col_name], target.columns[col_name]
            )
            if col_diff:
                td.modified_columns.append(col_diff)

        source_idx = set(source.indexes.keys())
        target_idx = set(target.indexes.keys())

        for idx_name in source_idx - target_idx:
            td.added_indexes.append(source.indexes[idx_name])

        for idx_name in target_idx - source_idx:
            td.dropped_indexes.append(target.indexes[idx_name])

        for idx_name in source_idx & target_idx:
            s_idx = source.indexes[idx_name]
            t_idx = target.indexes[idx_name]
            if set(s_idx.columns) != set(t_idx.columns) or s_idx.unique != t_idx.unique:
                td.dropped_indexes.append(t_idx)
                td.added_indexes.append(s_idx)

        source_uc = set(source.unique_constraints.keys())
        target_uc = set(target.unique_constraints.keys())

        for uc_name in source_uc - target_uc:
            td.added_unique_constraints.append(source.unique_constraints[uc_name])

        for uc_name in target_uc - source_uc:
            td.dropped_unique_constraints.append(target.unique_constraints[uc_name])

        for uc_name in source_uc & target_uc:
            s_uc = source.unique_constraints[uc_name]
            t_uc = target.unique_constraints[uc_name]
            if set(s_uc.columns) != set(t_uc.columns):
                td.dropped_unique_constraints.append(t_uc)
                td.added_unique_constraints.append(s_uc)

        source_fk = set(source.foreign_keys.keys())
        target_fk = set(target.foreign_keys.keys())

        for fk_name in source_fk - target_fk:
            td.added_foreign_keys.append(source.foreign_keys[fk_name])

        for fk_name in target_fk - source_fk:
            td.dropped_foreign_keys.append(target.foreign_keys[fk_name])

        for fk_name in source_fk & target_fk:
            s_fk = source.foreign_keys[fk_name]
            t_fk = target.foreign_keys[fk_name]
            if (
                set(s_fk.constrained_columns) != set(t_fk.constrained_columns)
                or s_fk.referred_table != t_fk.referred_table
                or set(s_fk.referred_columns) != set(t_fk.referred_columns)
            ):
                td.dropped_foreign_keys.append(t_fk)
                td.added_foreign_keys.append(s_fk)

        if set(source.primary_key_columns) != set(target.primary_key_columns):
            td.pk_changed = True
            td.source_pk = source.primary_key_columns
            td.target_pk = target.primary_key_columns

        return td

    def _diff_column(
        self,
        table_name: str,
        col_name: str,
        source: ColumnInfo,
        target: ColumnInfo,
    ) -> Optional[ColumnDiff]:
        if source.type_ != target.type_:
            return ColumnDiff(
                table_name=table_name,
                column_name=col_name,
                change_type="type",
                source_value=source.type_,
                target_value=target.type_,
            )
        return None


class AlterGenerator:
    def __init__(self, dialect: str = "mysql"):
        self.dialect = dialect
        self.statements: List[str] = []

    def generate(self, diff: SchemaDiff) -> List[str]:
        self.statements = []

        for table in diff.added_tables:
            self._generate_create_table(table)

        for table in diff.dropped_tables:
            self._generate_drop_table(table)

        for td in diff.table_diffs:
            self._generate_table_alter(td)

        return self.statements

    def _quote(self, identifier: str) -> str:
        if self.dialect in ("mysql",):
            return f"`{identifier}`"
        return f'"{identifier}"'

    def _generate_create_table(self, table: TableSchema) -> None:
        lines = []
        col_defs = []

        for col in table.columns.values():
            parts = [self._quote(col.name), col.type_]
            if not col.nullable:
                parts.append("NOT NULL")
            if col.default is not None:
                parts.append(f"DEFAULT {col.default}")
            if col.autoincrement:
                parts.append("AUTO_INCREMENT")
            col_defs.append(" ".join(parts))

        if table.primary_key_columns:
            pk_cols = ", ".join(self._quote(c) for c in table.primary_key_columns)
            col_defs.append(f"PRIMARY KEY ({pk_cols})")

        for uc in table.unique_constraints.values():
            uc_cols = ", ".join(self._quote(c) for c in uc.columns)
            col_defs.append(
                f"CONSTRAINT {self._quote(uc.name)} UNIQUE ({uc_cols})"
            )

        for fk in table.foreign_keys.values():
            fk_cols = ", ".join(self._quote(c) for c in fk.constrained_columns)
            ref_cols = ", ".join(self._quote(c) for c in fk.referred_columns)
            col_defs.append(
                f"CONSTRAINT {self._quote(fk.name)} FOREIGN KEY ({fk_cols}) "
                f"REFERENCES {self._quote(fk.referred_table)} ({ref_cols})"
            )

        body = ",\n  ".join(col_defs)
        self.statements.append(
            f"CREATE TABLE {self._quote(table.name)} (\n  {body}\n);"
        )

        for idx in table.indexes.values():
            idx_cols = ", ".join(self._quote(c) for c in idx.columns)
            unique = "UNIQUE " if idx.unique else ""
            self.statements.append(
                f"CREATE {unique}INDEX {self._quote(idx.name)} "
                f"ON {self._quote(table.name)} ({idx_cols});"
            )

    def _generate_drop_table(self, table: TableSchema) -> None:
        for fk in table.foreign_keys.values():
            self.statements.append(
                f"ALTER TABLE {self._quote(table.name)} "
                f"DROP FOREIGN KEY {self._quote(fk.name)};"
            )
        for idx in table.indexes.values():
            self.statements.append(f"DROP INDEX {self._quote(idx.name)};")
        self.statements.append(f"DROP TABLE {self._quote(table.name)};")

    def _generate_table_alter(self, td: TableDiff) -> None:
        tbl = self._quote(td.table_name)

        for fk in td.dropped_foreign_keys:
            self.statements.append(
                f"ALTER TABLE {tbl} DROP FOREIGN KEY {self._quote(fk.name)};"
            )

        for idx in td.dropped_indexes:
            if idx.unique:
                if self.dialect == "mysql":
                    self.statements.append(
                        f"ALTER TABLE {tbl} DROP INDEX {self._quote(idx.name)};"
                    )
                else:
                    self.statements.append(
                        f"DROP INDEX {self._quote(idx.name)};"
                    )
            else:
                self.statements.append(
                    f"DROP INDEX {self._quote(idx.name)};"
                )

        for uc in td.dropped_unique_constraints:
            if self.dialect == "mysql":
                self.statements.append(
                    f"ALTER TABLE {tbl} DROP INDEX {self._quote(uc.name)};"
                )
            else:
                self.statements.append(
                    f"ALTER TABLE {tbl} DROP CONSTRAINT {self._quote(uc.name)};"
                )

        if td.pk_changed:
            if td.target_pk:
                self.statements.append(
                    f"ALTER TABLE {tbl} DROP PRIMARY KEY;"
                )
            if td.source_pk:
                pk_cols = ", ".join(self._quote(c) for c in td.source_pk)
                self.statements.append(
                    f"ALTER TABLE {tbl} ADD PRIMARY KEY ({pk_cols});"
                )

        for col in td.dropped_columns:
            self.statements.append(
                f"ALTER TABLE {tbl} DROP COLUMN {self._quote(col.name)};"
            )

        for col in td.added_columns:
            parts = [f"ALTER TABLE {tbl} ADD COLUMN", self._quote(col.name), col.type_]
            if not col.nullable:
                parts.append("NOT NULL")
            if col.default is not None:
                parts.append(f"DEFAULT {col.default}")
            self.statements.append(" ".join(parts) + ";")

        for mod in td.modified_columns:
            self._generate_column_alter(td.table_name, mod)

        for uc in td.added_unique_constraints:
            uc_cols = ", ".join(self._quote(c) for c in uc.columns)
            self.statements.append(
                f"ALTER TABLE {tbl} ADD CONSTRAINT {self._quote(uc.name)} "
                f"UNIQUE ({uc_cols});"
            )

        for fk in td.added_foreign_keys:
            fk_cols = ", ".join(self._quote(c) for c in fk.constrained_columns)
            ref_cols = ", ".join(self._quote(c) for c in fk.referred_columns)
            self.statements.append(
                f"ALTER TABLE {tbl} ADD CONSTRAINT {self._quote(fk.name)} "
                f"FOREIGN KEY ({fk_cols}) REFERENCES {self._quote(fk.referred_table)} ({ref_cols});"
            )

        for idx in td.added_indexes:
            idx_cols = ", ".join(self._quote(c) for c in idx.columns)
            unique = "UNIQUE " if idx.unique else ""
            self.statements.append(
                f"CREATE {unique}INDEX {self._quote(idx.name)} ON {tbl} ({idx_cols});"
            )

    def _generate_column_alter(self, table_name: str, mod: ColumnDiff) -> None:
        tbl = self._quote(table_name)
        col = self._quote(mod.column_name)
        self.statements.append(
            f"ALTER TABLE {tbl} MODIFY COLUMN {col} {mod.source_value};"
        )


class HtmlReportGenerator:
    _CSS = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        background: #f5f7fa;
        color: #333;
        padding: 24px;
        line-height: 1.6;
    }
    .container { max-width: 1200px; margin: 0 auto; }
    header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        padding: 28px 32px;
        border-radius: 12px;
        margin-bottom: 24px;
        box-shadow: 0 4px 12px rgba(102, 126, 234, 0.3);
    }
    header h1 { font-size: 24px; margin-bottom: 8px; }
    header p { opacity: 0.9; font-size: 14px; }
    .stats {
        display: grid;
        grid-template-columns: repeat(4, 1fr);
        gap: 16px;
        margin-bottom: 24px;
    }
    .stat-card {
        background: white;
        padding: 20px;
        border-radius: 10px;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .stat-card .number { font-size: 32px; font-weight: 700; }
    .stat-card .label { font-size: 13px; color: #666; margin-top: 4px; }
    .stat-card.add .number { color: #10b981; }
    .stat-card.drop .number { color: #ef4444; }
    .stat-card.modify .number { color: #f59e0b; }
    .stat-card.total .number { color: #6366f1; }
    .section {
        background: white;
        border-radius: 10px;
        margin-bottom: 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        overflow: hidden;
    }
    .section-header {
        padding: 16px 20px;
        font-weight: 600;
        font-size: 16px;
        border-bottom: 1px solid #e5e7eb;
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .section-header .badge {
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 500;
        color: white;
    }
    .badge.add { background: #10b981; }
    .badge.drop { background: #ef4444; }
    .badge.modify { background: #f59e0b; }
    .table-item {
        padding: 16px 20px;
        border-bottom: 1px solid #f3f4f6;
    }
    .table-item:last-child { border-bottom: none; }
    .table-name {
        font-weight: 600;
        font-size: 15px;
        color: #1f2937;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .table-name .icon { font-size: 18px; }
    .diff-list { list-style: none; }
    .diff-list li {
        padding: 8px 12px;
        margin-bottom: 4px;
        border-radius: 6px;
        font-family: "JetBrains Mono", Consolas, Monaco, monospace;
        font-size: 13px;
    }
    .diff-list .add { background: #ecfdf5; color: #047857; }
    .diff-list .drop { background: #fef2f2; color: #b91c1c; }
    .diff-list .modify { background: #fffbeb; color: #b45309; }
    .diff-list .change-type {
        display: inline-block;
        min-width: 60px;
        font-weight: 600;
        font-size: 11px;
        text-transform: uppercase;
        margin-right: 8px;
    }
    details > summary {
        cursor: pointer;
        list-style: none;
    }
    details > summary::-webkit-details-marker { display: none; }
    details[open] .summary-arrow { transform: rotate(90deg); }
    .summary-arrow {
        display: inline-block;
        transition: transform 0.2s;
        margin-right: 6px;
    }
    .sql-block {
        background: #1e293b;
        color: #e2e8f0;
        padding: 16px;
        border-radius: 8px;
        font-family: "JetBrains Mono", Consolas, Monaco, monospace;
        font-size: 13px;
        line-height: 1.5;
        overflow-x: auto;
        white-space: pre-wrap;
        word-break: break-all;
    }
    .no-diff {
        text-align: center;
        padding: 60px 20px;
        color: #6b7280;
    }
    .no-diff .icon { font-size: 48px; margin-bottom: 12px; }
    .footer {
        text-align: center;
        padding: 20px;
        color: #9ca3af;
        font-size: 12px;
    }
    """

    def __init__(self, source_name: str = "源数据库", target_name: str = "目标数据库"):
        self.source_name = source_name
        self.target_name = target_name

    def generate(self, diff: SchemaDiff) -> str:
        parts: List[str] = []
        parts.append(self._html_header())
        parts.append(self._html_summary(diff))

        has_diff = bool(
            diff.added_tables or diff.dropped_tables or diff.table_diffs
        )

        if not has_diff:
            parts.append(self._html_no_diff())
        else:
            if diff.added_tables:
                parts.append(self._html_added_tables(diff.added_tables))
            if diff.dropped_tables:
                parts.append(self._html_dropped_tables(diff.dropped_tables))
            if diff.table_diffs:
                parts.append(self._html_modified_tables(diff.table_diffs))

        parts.append(self._html_footer())
        return "\n".join(parts)

    def save(self, diff: SchemaDiff, output_path: str) -> None:
        html = self.generate(diff)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

    def _escape(self, text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    def _html_header(self) -> str:
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>数据库表结构差异报告</title>
<style>{self._CSS}</style>
</head>
<body>
<div class="container">
<header>
  <h1>📊 数据库表结构差异报告</h1>
  <p>{self._escape(self.source_name)} → {self._escape(self.target_name)}</p>
</header>"""

    def _html_summary(self, diff: SchemaDiff) -> str:
        added = len(diff.added_tables)
        dropped = len(diff.dropped_tables)
        modified = len(diff.table_diffs)
        total = added + dropped + modified
        return f"""
<div class="stats">
  <div class="stat-card add">
    <div class="number">{added}</div>
    <div class="label">新增表</div>
  </div>
  <div class="stat-card drop">
    <div class="number">{dropped}</div>
    <div class="label">删除表</div>
  </div>
  <div class="stat-card modify">
    <div class="number">{modified}</div>
    <div class="label">修改表</div>
  </div>
  <div class="stat-card total">
    <div class="number">{total}</div>
    <div class="label">总计差异</div>
  </div>
</div>"""

    def _html_no_diff(self) -> str:
        return """
<div class="section">
  <div class="no-diff">
    <div class="icon">✅</div>
    <div style="font-size: 18px; font-weight: 600; margin-bottom: 8px;">两个数据库的表结构完全一致</div>
    <div>未检测到任何差异，无需同步。</div>
  </div>
</div>"""

    def _html_added_tables(self, tables: List[TableSchema]) -> str:
        items = []
        for t in tables:
            col_items = []
            for col in t.columns.values():
                col_items.append(
                    f'<li class="add"><span class="change-type">ADD</span>'
                    f'{self._escape(col.name)} {self._escape(col.type_)}</li>'
                )
            cols_html = "\n".join(col_items)
            items.append(
                f'<div class="table-item">'
                f'<div class="table-name"><span class="icon">➕</span>{self._escape(t.name)}</div>'
                f'<ul class="diff-list">{cols_html}</ul>'
                f"</div>"
            )
        body = "\n".join(items)
        return f"""
<div class="section">
  <div class="section-header">
    <span class="badge add">+ {len(tables)}</span>
    新增的表
  </div>
  {body}
</div>"""

    def _html_dropped_tables(self, tables: List[TableSchema]) -> str:
        items = []
        for t in tables:
            col_items = []
            for col in t.columns.values():
                col_items.append(
                    f'<li class="drop"><span class="change-type">DROP</span>'
                    f'{self._escape(col.name)} {self._escape(col.type_)}</li>'
                )
            cols_html = "\n".join(col_items)
            items.append(
                f'<div class="table-item">'
                f'<div class="table-name"><span class="icon">➖</span>{self._escape(t.name)}</div>'
                f'<ul class="diff-list">{cols_html}</ul>'
                f"</div>"
            )
        body = "\n".join(items)
        return f"""
<div class="section">
  <div class="section-header">
    <span class="badge drop">- {len(tables)}</span>
    删除的表
  </div>
  {body}
</div>"""

    def _html_modified_tables(self, table_diffs: List[TableDiff]) -> str:
        items = []
        for td in table_diffs:
            items.append(self._html_modified_table(td))
        body = "\n".join(items)
        return f"""
<div class="section">
  <div class="section-header">
    <span class="badge modify">~ {len(table_diffs)}</span>
    修改的表
  </div>
  {body}
</div>"""

    def _html_modified_table(self, td: TableDiff) -> str:
        diff_items: List[str] = []

        for col in td.added_columns:
            diff_items.append(
                f'<li class="add"><span class="change-type">ADD</span>'
                f'列 {self._escape(col.name)} {self._escape(col.type_)}</li>'
            )

        for col in td.dropped_columns:
            diff_items.append(
                f'<li class="drop"><span class="change-type">DROP</span>'
                f'列 {self._escape(col.name)} {self._escape(col.type_)}</li>'
            )

        for mod in td.modified_columns:
            diff_items.append(
                f'<li class="modify"><span class="change-type">MODIFY</span>'
                f'列 {self._escape(mod.column_name)}: '
                f'{self._escape(mod.target_value)} → {self._escape(mod.source_value)}</li>'
            )

        for idx in td.added_indexes:
            diff_items.append(
                f'<li class="add"><span class="change-type">ADD</span>'
                f'索引 {self._escape(idx.name)} ({", ".join(idx.columns)})'
                f'{" [唯一]" if idx.unique else ""}</li>'
            )

        for idx in td.dropped_indexes:
            diff_items.append(
                f'<li class="drop"><span class="change-type">DROP</span>'
                f'索引 {self._escape(idx.name)} ({", ".join(idx.columns)})'
                f'{" [唯一]" if idx.unique else ""}</li>'
            )

        if td.pk_changed:
            diff_items.append(
                f'<li class="modify"><span class="change-type">PK</span>'
                f'主键变更: {", ".join(td.target_pk)} → {", ".join(td.source_pk)}</li>'
            )

        diffs_html = "\n".join(diff_items)
        return (
            f'<div class="table-item">'
            f'<div class="table-name"><span class="icon">✏️</span>{self._escape(td.table_name)}</div>'
            f'<ul class="diff-list">{diffs_html}</ul>'
            f"</div>"
        )

    def _html_footer(self) -> str:
        return """
<div class="footer">
  Generated by Schema Sync Service
</div>
</div>
</body>
</html>"""


class SchemaSyncService:
    def __init__(self, source_url: str, target_url: str, dialect: str = "mysql"):
        self.source_url = source_url
        self.target_url = target_url
        self.dialect = dialect

    def sync(self, execute: bool = False) -> List[str]:
        source_engine = create_engine(self.source_url)
        target_engine = create_engine(self.target_url)

        source_inspector = DatabaseInspector(source_engine)
        target_inspector = DatabaseInspector(target_engine)

        source_schema = source_inspector.inspect_schema()
        target_schema = target_inspector.inspect_schema()

        differ = SchemaDiffer()
        diff = differ.diff(source_schema, target_schema)

        generator = AlterGenerator(dialect=self.dialect)
        statements = generator.generate(diff)

        if execute and statements:
            with target_engine.begin() as conn:
                for stmt in statements:
                    conn.execute(text(stmt))

        source_engine.dispose()
        target_engine.dispose()

        return statements

    def generate_report(self, output_path: str) -> None:
        source_engine = create_engine(self.source_url)
        target_engine = create_engine(self.target_url)

        source_inspector = DatabaseInspector(source_engine)
        target_inspector = DatabaseInspector(target_engine)

        source_schema = source_inspector.inspect_schema()
        target_schema = target_inspector.inspect_schema()

        differ = SchemaDiffer()
        diff = differ.diff(source_schema, target_schema)

        report_gen = HtmlReportGenerator(
            source_name=self.source_url,
            target_name=self.target_url,
        )
        report_gen.save(diff, output_path)

        source_engine.dispose()
        target_engine.dispose()

    @staticmethod
    def print_diff(statements: List[str]) -> None:
        if not statements:
            print("✅ 两个数据库的表结构完全一致，无需同步。")
            return

        print(f"📋 检测到 {len(statements)} 条差异，生成的 ALTER 语句如下：\n")
        for i, stmt in enumerate(statements, 1):
            print(f"-- {i}")
            print(stmt)
            print()


def main():
    parser = argparse.ArgumentParser(
        description="Schema Sync Service - 对比两个数据库的表结构差异并生成 ALTER 语句"
    )
    parser.add_argument(
        "--source",
        required=True,
        help="源数据库连接字符串 (如: mysql+pymysql://user:pass@host:3306/db1)",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="目标数据库连接字符串 (如: mysql+pymysql://user:pass@host:3306/db2)",
    )
    parser.add_argument(
        "--dialect",
        default="mysql",
        choices=["mysql", "postgresql", "sqlite"],
        help="数据库方言 (默认: mysql)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="是否直接在目标数据库上执行生成的 ALTER 语句",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=None,
        metavar="OUTPUT_PATH",
        help="导出 HTML 差异报告到指定文件路径 (如: report.html)",
    )

    args = parser.parse_args()

    service = SchemaSyncService(
        source_url=args.source,
        target_url=args.target,
        dialect=args.dialect,
    )

    statements = service.sync(execute=args.execute)
    SchemaSyncService.print_diff(statements)

    if args.report:
        service.generate_report(args.report)
        print(f"📄 HTML 差异报告已导出到: {args.report}")


if __name__ == "__main__":
    main()
