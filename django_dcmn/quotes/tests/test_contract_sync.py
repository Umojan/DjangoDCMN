"""The contract folder and the reference data are byte-identical to their copies in the hub (DESIGN §14.2).

The hub is the `WORK/DCMN` folder next to this repo on the dev machine (set `DCMN_HUB_DIR` elsewhere). Each test
is skipped when its hub path is absent, e.g. in CI, or before WP8 has copied the contract.
"""

import os
import unittest
from pathlib import Path

from crm.testing import NoNetworkTestCase
from quotes import contract

HUB_DIR = Path(os.environ.get('DCMN_HUB_DIR', '/Users/mac/WORK/DCMN'))
HUB_QUOTE = HUB_DIR / 'sprints' / 'sprint-14' / 'quote'
HUB_CONTRACT = HUB_QUOTE / 'contract'
HUB_DATA = HUB_QUOTE / 'proto' / 'src'   # the v5 source of truth read by design5/final/build.py


def contract_files(root: Path) -> dict[str, bytes]:
    """Relative path → bytes of the schema and every fixture under a contract folder."""
    paths = [root / contract.SCHEMA_PATH.name, *sorted((root / 'fixtures').glob('*.json'))]
    return {str(path.relative_to(root)): path.read_bytes() for path in paths if path.is_file()}


class HubCopyTests(NoNetworkTestCase):
    @unittest.skipUnless(HUB_CONTRACT.is_dir(), f'hub contract copy not found at {HUB_CONTRACT}')
    def test_schema_and_fixtures_identical_to_hub_copy(self):
        ours, theirs = contract_files(contract.CONTRACT_DIR), contract_files(HUB_CONTRACT)

        self.assertEqual(sorted(theirs), sorted(ours))
        for name, data in ours.items():
            with self.subTest(file=name):
                self.assertEqual(theirs[name], data, f'{name} differs from the hub copy')


class DataCopyTests(NoNetworkTestCase):
    @unittest.skipUnless(HUB_DATA.is_dir(), f'hub v5 source not found at {HUB_DATA}')
    def test_languages_and_countries_identical_to_v5_source(self):
        for name in ('languages.json', 'countries.json'):
            with self.subTest(file=name):
                self.assertEqual((contract.DATA_DIR / name).read_bytes(), (HUB_DATA / name).read_bytes())
