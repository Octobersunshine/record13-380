"""
Unit tests for SchemaSyncService - using in-memory SQLite databases.
"""

import unittest

from sqlalchemy import create_engine, text

from schema_sync import (
    AlterGenerator,
    DatabaseInspector,
    HtmlReportGenerator,
    SchemaDiffer,
    SchemaSyncService,
)


def _build_source_db() -> "Engine":
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100) NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    age INTEGER DEFAULT 0,
                    created_at VARCHAR(50)
                )
                """
            )
        )
        conn.execute(text("CREATE UNIQUE INDEX ix_users_email ON users (email)"))
        conn.execute(
            text(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_orders_user_id ON orders (user_id)"))
    return engine


def _build_target_db() -> "Engine":
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(50),
                    email VARCHAR(255) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100) NOT NULL,
                    price INTEGER NOT NULL
                )
                """
            )
        )
    return engine


def _build_same_columns_different_order_db() -> "Engine":
    """构建列顺序不同但列名和类型完全相同的数据库，用于验证顺序不影响对比结果"""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at VARCHAR(50),
                    age INTEGER DEFAULT 0,
                    email VARCHAR(255) NOT NULL,
                    name VARCHAR(100) NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE UNIQUE INDEX ix_users_email ON users (email)"))
        conn.execute(
            text(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    amount INTEGER NOT NULL DEFAULT 0,
                    user_id INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_orders_user_id ON orders (user_id)"))
    return engine


def _build_same_type_different_nullable_default_db() -> "Engine":
    """构建列名和类型相同但 nullable/default 不同的数据库，验证这些差异被忽略"""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100),
                    email VARCHAR(255),
                    age INTEGER,
                    created_at VARCHAR(50) DEFAULT 'now'
                )
                """
            )
        )
        conn.execute(text("CREATE UNIQUE INDEX ix_users_email ON users (email)"))
        conn.execute(
            text(
                """
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX ix_orders_user_id ON orders (user_id)"))
    return engine


class TestDatabaseInspector(unittest.TestCase):
    def test_inspect_source(self):
        engine = _build_source_db()
        inspector = DatabaseInspector(engine)
        schema = inspector.inspect_schema()

        self.assertIn("users", schema.tables)
        self.assertIn("orders", schema.tables)

        users = schema.tables["users"]
        self.assertIn("id", users.columns)
        self.assertIn("name", users.columns)
        self.assertIn("email", users.columns)
        self.assertIn("age", users.columns)

        self.assertFalse(users.columns["name"].nullable)
        self.assertTrue(users.columns["age"].nullable)
        self.assertEqual(users.columns["age"].default, "0")

    def test_inspect_target(self):
        engine = _build_target_db()
        inspector = DatabaseInspector(engine)
        schema = inspector.inspect_schema()

        self.assertIn("users", schema.tables)
        self.assertIn("products", schema.tables)
        self.assertNotIn("orders", schema.tables)


class TestSchemaDiffer(unittest.TestCase):
    def setUp(self):
        src_engine = _build_source_db()
        tgt_engine = _build_target_db()

        src_inspector = DatabaseInspector(src_engine)
        tgt_inspector = DatabaseInspector(tgt_engine)

        self.source = src_inspector.inspect_schema()
        self.target = tgt_inspector.inspect_schema()

        differ = SchemaDiffer()
        self.diff = differ.diff(self.source, self.target)

    def test_added_tables(self):
        added_names = [t.name for t in self.diff.added_tables]
        self.assertIn("orders", added_names)
        self.assertNotIn("products", added_names)

    def test_dropped_tables(self):
        dropped_names = [t.name for t in self.diff.dropped_tables]
        self.assertIn("products", dropped_names)
        self.assertNotIn("orders", dropped_names)

    def test_added_columns(self):
        users_diff = next(
            (td for td in self.diff.table_diffs if td.table_name == "users"), None
        )
        self.assertIsNotNone(users_diff)
        added_col_names = [c.name for c in users_diff.added_columns]
        self.assertIn("age", added_col_names)
        self.assertIn("created_at", added_col_names)

    def test_dropped_columns(self):
        pass

    def test_modified_columns(self):
        users_diff = next(
            (td for td in self.diff.table_diffs if td.table_name == "users"), None
        )
        self.assertIsNotNone(users_diff)
        mod_names = [m.column_name for m in users_diff.modified_columns]
        self.assertIn("name", mod_names)

    def test_added_indexes(self):
        users_diff = next(
            (td for td in self.diff.table_diffs if td.table_name == "users"), None
        )
        if users_diff:
            added_idx_names = [idx.name for idx in users_diff.added_indexes]
            self.assertIn("ix_users_email", added_idx_names)

    def test_column_order_does_not_matter(self):
        """字段顺序不同，但列名和类型完全相同时，不应产生任何差异"""
        src_engine = _build_source_db()
        tgt_engine = _build_same_columns_different_order_db()

        src_schema = DatabaseInspector(src_engine).inspect_schema()
        tgt_schema = DatabaseInspector(tgt_engine).inspect_schema()

        diff = SchemaDiffer().diff(src_schema, tgt_schema)

        self.assertEqual(len(diff.added_tables), 0)
        self.assertEqual(len(diff.dropped_tables), 0)
        self.assertEqual(len(diff.table_diffs), 0)

    def test_nullable_and_default_ignored(self):
        """列名和类型相同，但 nullable/default 不同时，不应产生列修改差异"""
        src_engine = _build_source_db()
        tgt_engine = _build_same_type_different_nullable_default_db()

        src_schema = DatabaseInspector(src_engine).inspect_schema()
        tgt_schema = DatabaseInspector(tgt_engine).inspect_schema()

        diff = SchemaDiffer().diff(src_schema, tgt_schema)

        self.assertEqual(len(diff.added_tables), 0)
        self.assertEqual(len(diff.dropped_tables), 0)

        users_diff = next(
            (td for td in diff.table_diffs if td.table_name == "users"), None
        )
        if users_diff:
            self.assertEqual(len(users_diff.modified_columns), 0)
            self.assertEqual(len(users_diff.added_columns), 0)
            self.assertEqual(len(users_diff.dropped_columns), 0)


class TestAlterGenerator(unittest.TestCase):
    def setUp(self):
        src_engine = _build_source_db()
        tgt_engine = _build_target_db()

        src_inspector = DatabaseInspector(src_engine)
        tgt_inspector = DatabaseInspector(tgt_engine)

        self.source = src_inspector.inspect_schema()
        self.target = tgt_inspector.inspect_schema()

        differ = SchemaDiffer()
        self.diff = differ.diff(self.source, self.target)

    def test_generate_creates_statements(self):
        generator = AlterGenerator(dialect="mysql")
        statements = generator.generate(self.diff)
        self.assertGreater(len(statements), 0)

    def test_create_table_for_added(self):
        generator = AlterGenerator(dialect="mysql")
        statements = generator.generate(self.diff)
        create_order = [s for s in statements if "CREATE TABLE" in s and "orders" in s]
        self.assertGreater(len(create_order), 0)

    def test_drop_table_for_removed(self):
        generator = AlterGenerator(dialect="mysql")
        statements = generator.generate(self.diff)
        drop_product = [s for s in statements if "DROP TABLE" in s and "products" in s]
        self.assertGreater(len(drop_product), 0)

    def test_add_column_statement(self):
        generator = AlterGenerator(dialect="mysql")
        statements = generator.generate(self.diff)
        add_age = [s for s in statements if "ADD COLUMN" in s and "age" in s]
        self.assertGreater(len(add_age), 0)

    def test_no_diff_same_schema(self):
        engine = _build_source_db()
        inspector = DatabaseInspector(engine)
        schema = inspector.inspect_schema()

        differ = SchemaDiffer()
        diff = differ.diff(schema, schema)

        generator = AlterGenerator(dialect="mysql")
        statements = generator.generate(diff)
        self.assertEqual(len(statements), 0)

    def test_no_statements_when_only_order_differs(self):
        """列顺序不同时，不应生成任何 ALTER 语句"""
        src_engine = _build_source_db()
        tgt_engine = _build_same_columns_different_order_db()

        src_schema = DatabaseInspector(src_engine).inspect_schema()
        tgt_schema = DatabaseInspector(tgt_engine).inspect_schema()

        diff = SchemaDiffer().diff(src_schema, tgt_schema)
        statements = AlterGenerator(dialect="mysql").generate(diff)
        self.assertEqual(len(statements), 0)


class TestSchemaSyncService(unittest.TestCase):
    def test_sync_dry_run(self):
        src_engine = _build_source_db()
        tgt_engine = _build_target_db()

        service = SchemaSyncService(
            source_url="sqlite://",
            target_url="sqlite://",
            dialect="mysql",
        )

        source_inspector = DatabaseInspector(src_engine)
        target_inspector = DatabaseInspector(tgt_engine)

        source_schema = source_inspector.inspect_schema()
        target_schema = target_inspector.inspect_schema()

        differ = SchemaDiffer()
        diff = differ.diff(source_schema, target_schema)

        generator = AlterGenerator(dialect="mysql")
        statements = generator.generate(diff)

        self.assertGreater(len(statements), 0)

    def test_print_diff_no_statements(self):
        import io
        import sys

        captured = io.StringIO()
        sys.stdout = captured
        try:
            SchemaSyncService.print_diff([])
        finally:
            sys.stdout = sys.__stdout__

        self.assertIn("完全一致", captured.getvalue())


class TestHtmlReportGenerator(unittest.TestCase):
    def setUp(self):
        src_engine = _build_source_db()
        tgt_engine = _build_target_db()

        src_inspector = DatabaseInspector(src_engine)
        tgt_inspector = DatabaseInspector(tgt_engine)

        self.source = src_inspector.inspect_schema()
        self.target = tgt_inspector.inspect_schema()

        differ = SchemaDiffer()
        self.diff = differ.diff(self.source, self.target)

    def test_generate_html_returns_string(self):
        generator = HtmlReportGenerator()
        html = generator.generate(self.diff)
        self.assertIsInstance(html, str)
        self.assertGreater(len(html), 0)

    def test_html_contains_doctype(self):
        generator = HtmlReportGenerator()
        html = generator.generate(self.diff)
        self.assertIn("<!DOCTYPE html>", html)

    def test_html_contains_title(self):
        generator = HtmlReportGenerator()
        html = generator.generate(self.diff)
        self.assertIn("数据库表结构差异报告", html)

    def test_html_contains_added_tables(self):
        generator = HtmlReportGenerator()
        html = generator.generate(self.diff)
        self.assertIn("新增的表", html)
        self.assertIn("orders", html)

    def test_html_contains_dropped_tables(self):
        generator = HtmlReportGenerator()
        html = generator.generate(self.diff)
        self.assertIn("删除的表", html)
        self.assertIn("products", html)

    def test_html_contains_modified_tables(self):
        generator = HtmlReportGenerator()
        html = generator.generate(self.diff)
        self.assertIn("修改的表", html)
        self.assertIn("users", html)

    def test_html_no_diff_scenario(self):
        """没有差异时，应显示『完全一致』的提示"""
        src_engine = _build_source_db()
        schema = DatabaseInspector(src_engine).inspect_schema()
        diff = SchemaDiffer().diff(schema, schema)

        generator = HtmlReportGenerator()
        html = generator.generate(diff)
        self.assertIn("完全一致", html)

    def test_html_order_different_no_diff(self):
        """列顺序不同时，HTML 报告也应显示无差异"""
        src_engine = _build_source_db()
        tgt_engine = _build_same_columns_different_order_db()

        src_schema = DatabaseInspector(src_engine).inspect_schema()
        tgt_schema = DatabaseInspector(tgt_engine).inspect_schema()
        diff = SchemaDiffer().diff(src_schema, tgt_schema)

        generator = HtmlReportGenerator()
        html = generator.generate(diff)
        self.assertIn("完全一致", html)

    def test_save_to_file(self):
        import os
        import tempfile

        generator = HtmlReportGenerator()
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w"
        ) as f:
            tmp_path = f.name

        try:
            generator.save(self.diff, tmp_path)
            self.assertTrue(os.path.exists(tmp_path))
            with open(tmp_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("<!DOCTYPE html>", content)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_html_escapes_special_characters(self):
        """验证特殊字符被正确转义"""
        src_engine = _build_source_db()
        src_schema = DatabaseInspector(src_engine).inspect_schema()

        tgt_engine = create_engine("sqlite:///:memory:")
        with tgt_engine.begin() as conn:
            conn.execute(
                text(
                    'CREATE TABLE "test<>&table" ('
                    "id INTEGER PRIMARY KEY, "
                    '"col<>&name" VARCHAR(100)'
                    ")"
                )
            )
        tgt_schema = DatabaseInspector(tgt_engine).inspect_schema()

        diff = SchemaDiffer().diff(src_schema, tgt_schema)
        generator = HtmlReportGenerator()
        html = generator.generate(diff)

        self.assertIn("&lt;", html)
        self.assertIn("&gt;", html)
        self.assertIn("&amp;", html)


if __name__ == "__main__":
    unittest.main()
