"""Unit tests for the category-tree CRUD layer.

Covers depth/cycle helpers, BFS-based subtree walks, and the bulk-reorder
validator. PR1 of the categories overhaul plan
(``ecommerce_store/.claude/notes/plans/admin/in-progress/2026-05-21-categories-overhaul.md``).
"""

from __future__ import annotations

import pytest

from app.crud import category as crud_category
from app.crud.category import (
    MAX_DEPTH,
    CategoryCycleError,
    CategoryDepthExceededError,
    CategoryParentMissingError,
)

pytestmark = pytest.mark.integration


def _make_chain(category_factory, levels: int):
    """Build a parent → child chain ``levels`` deep, return [root, ..., leaf]."""
    nodes = []
    parent_id = None
    for i in range(levels):
        node = category_factory(name=f"L{i}", slug=f"l{i}", parent_id=parent_id)
        nodes.append(node)
        parent_id = node.id
    return nodes


# ---------------------------------------------------------------------------
# depth_of
# ---------------------------------------------------------------------------


def test_depth_of_returns_one_for_none_parent(db_session):
    assert crud_category.depth_of(db_session, parent_id=None) == 1


def test_depth_of_root_returns_two(db_session, category_factory):
    root = category_factory(slug="root-d")
    assert crud_category.depth_of(db_session, parent_id=root.id) == 2


def test_depth_of_grandchild_position_returns_three(db_session, category_factory):
    _root, sub = _make_chain(category_factory, 2)
    assert crud_category.depth_of(db_session, parent_id=sub.id) == 3


def test_depth_of_below_leaf_at_max_depth_exceeds_cap(db_session, category_factory):
    _, _, leaf = _make_chain(category_factory, 3)
    assert crud_category.depth_of(db_session, parent_id=leaf.id) == 4
    assert crud_category.depth_of(db_session, parent_id=leaf.id) > MAX_DEPTH


# ---------------------------------------------------------------------------
# get_ancestors / is_descendant
# ---------------------------------------------------------------------------


def test_get_ancestors_walks_from_leaf_to_root(db_session, category_factory):
    root, sub, leaf = _make_chain(category_factory, 3)
    ancestors = crud_category.get_ancestors(db_session, category_id=leaf.id)
    assert [a.id for a in ancestors] == [sub.id, root.id]


def test_get_ancestors_empty_for_root(db_session, category_factory):
    root = category_factory(slug="solo-root")
    assert crud_category.get_ancestors(db_session, category_id=root.id) == []


def test_is_descendant_true_for_self(db_session, category_factory):
    root = category_factory(slug="self-dec")
    assert crud_category.is_descendant(db_session, candidate_id=root.id, of_id=root.id) is True


def test_is_descendant_true_for_grandchild(db_session, category_factory):
    root, _sub, leaf = _make_chain(category_factory, 3)
    assert crud_category.is_descendant(db_session, candidate_id=leaf.id, of_id=root.id) is True


def test_is_descendant_false_for_sibling(db_session, category_factory):
    root = category_factory(slug="iso-root")
    sib1 = category_factory(slug="sib-a", parent_id=root.id)
    sib2 = category_factory(slug="sib-b", parent_id=root.id)
    assert crud_category.is_descendant(db_session, candidate_id=sib1.id, of_id=sib2.id) is False


# ---------------------------------------------------------------------------
# subtree_height
# ---------------------------------------------------------------------------


def test_subtree_height_zero_for_leaf(db_session, category_factory):
    leaf = category_factory(slug="lonely-leaf")
    assert crud_category.subtree_height(db_session, root_id=leaf.id) == 0


def test_subtree_height_counts_deepest_branch(db_session, category_factory):
    root = category_factory(slug="sh-root")
    a = category_factory(slug="sh-a", parent_id=root.id)
    category_factory(slug="sh-a-1", parent_id=a.id)
    b = category_factory(slug="sh-b", parent_id=root.id)
    c = category_factory(slug="sh-b-1", parent_id=b.id)
    category_factory(slug="sh-b-1-1", parent_id=c.id)
    assert crud_category.subtree_height(db_session, root_id=root.id) == 3


# ---------------------------------------------------------------------------
# count_subtree_products / count_direct_products
# ---------------------------------------------------------------------------


def test_count_subtree_products_sums_across_descendants(
    db_session, category_factory, product_factory
):
    root = category_factory(slug="cp-root")
    sub = category_factory(slug="cp-sub", parent_id=root.id)
    product_factory(slug="cp-p1", category=root)
    product_factory(slug="cp-p2", category=sub)
    product_factory(slug="cp-p3", category=sub, is_active=False)
    assert crud_category.count_subtree_products(db_session, root_id=root.id) == 2


def test_count_direct_products_ignores_descendants(db_session, category_factory, product_factory):
    root = category_factory(slug="cd-root")
    sub = category_factory(slug="cd-sub", parent_id=root.id)
    product_factory(slug="cd-p1", category=sub)
    assert crud_category.count_direct_products(db_session, category_id=root.id) == 0


# ---------------------------------------------------------------------------
# bulk_reorder — validation
# ---------------------------------------------------------------------------


def test_bulk_reorder_happy_path(db_session, category_factory):
    a = category_factory(slug="br-a", sort_order=0)
    b = category_factory(slug="br-b", sort_order=1)
    c = category_factory(slug="br-c", sort_order=2)
    crud_category.bulk_reorder(
        db_session,
        items=[(a.id, None, 2), (b.id, None, 0), (c.id, None, 1)],
    )
    db_session.commit()
    db_session.refresh(a)
    db_session.refresh(b)
    db_session.refresh(c)
    assert (a.sort_order, b.sort_order, c.sort_order) == (2, 0, 1)


def test_bulk_reorder_rejects_self_parent(db_session, category_factory):
    a = category_factory(slug="br-cyc-a")
    with pytest.raises(CategoryCycleError):
        crud_category.bulk_reorder(db_session, items=[(a.id, a.id, 0)])


def test_bulk_reorder_rejects_cycle_via_descendant(db_session, category_factory):
    root, _sub, leaf = _make_chain(category_factory, 3)
    # Move root under leaf → leaf → root forms a cycle.
    with pytest.raises(CategoryCycleError):
        crud_category.bulk_reorder(db_session, items=[(root.id, leaf.id, 0)])


def test_bulk_reorder_rejects_depth_violation(db_session, category_factory):
    _, _, leaf = _make_chain(category_factory, 3)
    extra = category_factory(slug="br-extra")
    # Moving extra under leaf puts extra at depth 4.
    with pytest.raises(CategoryDepthExceededError):
        crud_category.bulk_reorder(db_session, items=[(extra.id, leaf.id, 0)])


def test_bulk_reorder_rejects_descendants_pushed_past_cap(db_session, category_factory):
    # X is depth 2 with child Y at depth 3. Moving X under another depth-2 node
    # puts X at depth 3, Y at depth 4 → reject.
    root_a = category_factory(slug="br-ra")
    root_b = category_factory(slug="br-rb")
    inner_b = category_factory(slug="br-rb-1", parent_id=root_b.id)
    x = category_factory(slug="br-x", parent_id=root_a.id)
    category_factory(slug="br-y", parent_id=x.id)
    with pytest.raises(CategoryDepthExceededError):
        crud_category.bulk_reorder(db_session, items=[(x.id, inner_b.id, 0)])


def test_bulk_reorder_rejects_missing_id(db_session, category_factory):
    a = category_factory(slug="br-miss-a")
    with pytest.raises(CategoryParentMissingError):
        crud_category.bulk_reorder(db_session, items=[(a.id, 999_999, 0)])


def test_bulk_reorder_rolls_back_on_violation(db_session, category_factory):
    a = category_factory(slug="br-rb-a", sort_order=0)
    b = category_factory(slug="br-rb-b", sort_order=0)
    # First item valid, second introduces a self-parent cycle.
    with pytest.raises(CategoryCycleError):
        crud_category.bulk_reorder(
            db_session,
            items=[(a.id, None, 5), (b.id, b.id, 0)],
        )
    db_session.rollback()
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.sort_order == 0
    assert b.parent_id is None


# ---------------------------------------------------------------------------
# delete_subtree / reparent_children_to_root
# ---------------------------------------------------------------------------


def test_delete_subtree_returns_deepest_first(db_session, category_factory):
    root, sub, leaf = _make_chain(category_factory, 3)
    deleted = crud_category.delete_subtree(db_session, root=root)
    db_session.commit()
    assert [c.id for c in deleted] == [leaf.id, sub.id, root.id]


def test_reparent_children_to_root_sets_null(db_session, category_factory):
    root = category_factory(slug="rp-root")
    a = category_factory(slug="rp-a", parent_id=root.id)
    b = category_factory(slug="rp-b", parent_id=root.id)
    n = crud_category.reparent_children_to_root(db_session, parent_id=root.id)
    db_session.commit()
    db_session.refresh(a)
    db_session.refresh(b)
    assert n == 2
    assert a.parent_id is None
    assert b.parent_id is None
