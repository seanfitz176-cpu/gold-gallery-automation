#!/usr/bin/env python3
"""Look up real, currently-valid eBay leaf category IDs for this app's
marketplace, by free-text search. Read-only — makes no eBay changes.

Usage:
    python find_category.py "mobile phone"

Category IDs are marketplace-specific (an id valid for EBAY_US is often not
valid for EBAY_GB), and eBay does not document a fixed list — this queries
eBay's own Taxonomy API instead of guessing. Only a suggestion whose
"categoryTreeNodeLevel" makes it a leaf can be used directly in an Offer;
non-leaf (parent) categories cannot.
"""

from __future__ import annotations

import sys

from ebay_app.config import ConfigError, load_config
from ebay_app.ebay_client import EbayError, EbaySandboxClient
from ebay_app.storage import AppDatabase, StorageError


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print('Usage: python find_category.py "search term"')
        print('Example: python find_category.py "mobile phone"')
        return 1
    query = sys.argv[1].strip()

    database = None
    try:
        config = load_config()
        database = AppDatabase(config.database_path, config.data_encryption_key)
        client = EbaySandboxClient(config, database)

        tree_id = client.get_default_category_tree_id()
        print(f"Category tree for {config.marketplace_id}: {tree_id}\n")

        suggestions = client.get_category_suggestions(tree_id, query)
        if not suggestions:
            print(f"No category suggestions found for '{query}'. Try a different term.")
            return 1

        print(f"Suggestions for '{query}':\n")
        # A suggestion can be a parent category, not just a leaf -- and only a
        # leaf category can be used to list an item. Check each of the first
        # few suggestions against eBay directly rather than assuming.
        checked = 0
        leaf_found = False
        for item in suggestions:
            category = item.get("category", {})
            category_id = category.get("categoryId", "?")
            category_name = category.get("categoryName", "?")
            ancestors = item.get("categoryTreeNodeAncestors", [])
            path = " > ".join(
                a.get("categoryName", "?") for a in reversed(ancestors)
            )
            leaf_note = ""
            if checked < 5 and category_id != "?":
                checked += 1
                try:
                    subtree = client.get_category_subtree(tree_id, category_id)
                    node = subtree.get("categorySubtreeNode", {})
                    is_leaf = node.get("leafCategoryTreeNode")
                    if is_leaf is True:
                        leaf_note = "  <-- LEAF, usable for a listing"
                        leaf_found = True
                    elif is_leaf is False:
                        leaf_note = "  <-- NOT a leaf (has subcategories); do not use this one"
                    else:
                        leaf_note = f"  <-- leaf status unclear, raw node: {node!r}"
                except EbayError as subtree_error:
                    leaf_note = f"  <-- could not check leaf status: {subtree_error}"
            print(f"  ID {category_id}: {category_name}{leaf_note}")
            if path:
                print(f"    (under: {path})")
        if leaf_found:
            print(
                "\nUse one of the IDs marked LEAF above as the 'eBay category ID' "
                "when preparing a draft. Do not use one marked NOT a leaf."
            )
        else:
            print(
                "\nNone of the checked suggestions were confirmed as a leaf category. "
                "Try a more specific search term (e.g. a particular phone brand) so a "
                "leaf category is suggested."
            )
        return 0
    except (ConfigError, StorageError, EbayError) as error:
        print(f"Category lookup: FAILED — {error}")
        if isinstance(error, EbayError):
            print(f"  HTTP status: {error.status}")
            print(f"  Code: {error.code}")
            if error.details:
                for item in error.details:
                    print(f"  eBay error: {item}")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
