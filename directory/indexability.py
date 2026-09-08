"""Which browse pages a crawler should index.

One rule, one place. The category page view, its template tag, and the sitemap
generator all call ``category_indexable`` -- there is no second copy of the
threshold (spec §12.2, and the "Category page 1 indexable; page 2+, thin
categories and search noindex" decision).
"""

from __future__ import annotations

# Minimum published listings for a category browse page to be worth indexing.
# A module constant, deliberately not a tenant setting -- §12.2 gives the
# principle (thin combination pages damage standing), tenants do not tune it.
MIN_INDEXABLE_LISTINGS = 3


def category_indexable(published_count: int, page: int = 1) -> bool:
    """True when a category browse page should be indexed: page 1, and the
    category has at least ``MIN_INDEXABLE_LISTINGS`` published listings.

    Page 2+ and thin categories get ``noindex,follow`` on the page and are
    absent from the sitemap.
    """
    return page == 1 and published_count >= MIN_INDEXABLE_LISTINGS
