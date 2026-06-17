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
            if s_idx.columns != t_idx.columns or s_idx.unique != t_idx.unique:
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
            if s_uc.columns != t_uc.columns:
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
                s_fk.constrained_columns != t_fk.constrained_columns
                or s_fk.referred_table != t_fk.referred_table
                or s_fk.referred_columns != t_fk.referred_columns
            ):
                td.dropped_foreign_keys.append(t_fk)
                td.added_foreign_keys.append(s_fk)

        if source.primary_key_columns != target.primary_key_columns:
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
        changes = []

        if source.type_ != target.type_:
            changes.append(
                ColumnDiff(
                    table_name=table_name,
                    column_name=col_name,
                    change_type="type",
                    source_value=source.type_,
                    target_value=target.type_,
                )
            )

        if source.nullable != target.nullable:
            changes.append(
                ColumnDiff(
                    table_name=table_name,
                    column_name=col_name,
                    change_type="nullable",
                    source_value=source.nullable,
                    target_value=target.nullable,
                )
            )

        if source.default != target.default:
            changes.append(
                ColumnDiff(
                    table_name=table_name,
                    column_name=col_name,
                    change_type="default",
                    source_value=source.default,
                    target_value=target.default,
                )
            )

        if len(changes) == 1:
            return changes[0]
        if len(changes) > 1:
            cd = ColumnDiff(
                table_name=table_name,
                column_name=col_name,
                change_type="compound",
            )
            cd.sub_diffs = changes
            return cd
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

    def _generate_column_alter(self, table_name: str, mod: Any) -> None:
        tbl = self._quote(table_name)
        col = self._quote(
            mod.column_name if hasattr(mod, "column_name") else ""
        )

        if hasattr(mod, "sub_diffs"):
            for sub in mod.sub_diffs:
                self._generate_column_alter(table_name, sub)
            return

        if mod.change_type == "type":
            self.statements.append(
                f"ALTER TABLE {tbl} MODIFY COLUMN {col} {mod.source_value};"
            )
        elif mod.change_type == "nullable":
            if mod.source_value:
                self.statements.append(
                    f"ALTER TABLE {tbl} MODIFY COLUMN {col} NULL;"
                )
            else:
                self.statements.append(
                    f"ALTER TABLE {tbl} MODIFY COLUMN {col} NOT NULL;"
                )
        elif mod.change_type == "default":
            if mod.source_value is not None:
                self.statements.append(
                    f"ALTER TABLE {tbl} ALTER COLUMN {col} SET DEFAULT {mod.source_value};"
                )
            else:
                self.statements.append(
                    f"ALTER TABLE {tbl} ALTER COLUMN {col} DROP DEFAULT;"
                )


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

    args = parser.parse_args()

    service = SchemaSyncService(
        source_url=args.source,
        target_url=args.target,
        dialect=args.dialect,
    )

    statements = service.sync(execute=args.execute)
    SchemaSyncService.print_diff(statements)


if __name__ == "__main__":
    main()
