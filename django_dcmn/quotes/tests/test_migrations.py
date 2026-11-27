"""The new migrations depend only on the orders migration already applied on prod (WP1, plan §0 rule 3)."""

from django.db.migrations.loader import MigrationLoader

from crm.testing import NoNetworkTestCase

# Latest orders migration applied on prod; confirm with `showmigrations orders` at S0 before changing it.
PROD_ORDERS_MIGRATION = '0035_application'
NEW_APPS = {'quotes', 'crm'}


class DependencyTests(NoNetworkTestCase):
    def test_only_prod_orders_migration_and_quotes(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        prod_orders = ('orders', PROD_ORDERS_MIGRATION)
        self.assertIn(prod_orders, loader.disk_migrations)

        self.assertEqual(loader.get_migration('quotes', '0001_initial').dependencies, [prod_orders])
        self.assertEqual(
            set(loader.get_migration('crm', '0001_initial').dependencies),
            {prod_orders, ('quotes', '0001_initial')},
        )

        new_migrations = [key for key in loader.disk_migrations if key[0] in NEW_APPS]
        for key in new_migrations:
            for dependency in loader.disk_migrations[key].dependencies:
                with self.subTest(migration=key, dependency=dependency):
                    self.assertTrue(dependency[0] in NEW_APPS or dependency == prod_orders)

        # `migrate <app> --plan` on prod: everything up to the prod orders migration is applied already,
        # so the plan may contain quotes and crm migrations only.
        applied_on_prod = set(loader.graph.forwards_plan(prod_orders))
        for key in new_migrations:
            pending = [node for node in loader.graph.forwards_plan(key) if node not in applied_on_prod]
            self.assertEqual({app for app, _ in pending} - NEW_APPS, set(), key)
