"""
Bulk recipient import: preview before writing anything, exact-duplicate
skip (case-insensitive, internal-whitespace-collapsed — makes repeated
runs idempotent), spelling variations flagged for review but never
auto-merged, one transaction per execute so a failure never leaves
partial records. Same philosophy as the Phase 4 legacy-data migration
tool: schema ships automatically, business data judgment calls go through
this deliberate, previewable, audited path.

Exact-duplicate detection deliberately looks at EVERY customer row —
active, inactive, temporary, and merged-away alike — never just the
active ones. A name that already exists anywhere in history must not be
recreated just because the original record was later deactivated or
merged. Comparison uses Customer.normalized_name (case-folded, internal
whitespace collapsed); the exact spelling supplied is always what gets
stored — normalization is only ever a comparison key, never a display
value.
"""
import difflib

from sqlalchemy.exc import IntegrityError

from webapp.extensions import db
from webapp.models.customer import Customer, normalize_name
from webapp.models.sales_category import SalesCategory
from webapp.services.customer_service import DUPLICATE_SIMILARITY_THRESHOLD, find_similar_customers

# The 5 initial recipient assignments from the enhancement request.
INITIAL_ASSIGNMENTS = [
    ("Dakar", "Metro Sales"),
    ("Derrick", "Metro Sales"),
    ("Ayub", "Upcountry Sales"),
    ("Fenecansi", "Upcountry Sales"),
    ("Shop", "Shop/Kikuubo Sales"),
]

# The authoritative Corporate Sales customer list, exactly as supplied —
# names are NOT spell-corrected here (e.g. "Kenjoy Supermrket Nansana" is
# intentionally kept as given; see the spelling-variation preview instead).
CORPORATE_SALES_NAMES = [
    "Dynasty Lounge", "Sokoni Africa LTD", "Lazo", "Makutano", "Element Bar & Grill",
    "Tales Bar & Lounge", "Atro Group", "Oak Cafe LTD", "Oak Cafe Bakery",
    "Standard Supermarket Old Park", "Pack Max", "Italian Mart aka Emburara",
    "S&S Supermarket Kasanga", "S&S Supermarket Entebbe", "Kenjoy Supermarket",
    "Easy Save", "Benco Liners LTD", "Sombe Supermarket", "Paris Corner Supermarket",
    "Portbell Supermarket Kireka", "City Joy Supermarket", "Masters Supermarket Gayaza",
    "Shopwise Retail LTD", "Starview Hotel", "Brood", "Hotel International",
    "Nawab Gardens", "Fine Diners", "Hotel Africana", "BMK House", "Cynibel Supermarket",
    "Quality International School", "Senana Hypermarket LTD", "Ashraf Stores",
    "Richard's Shop", "Ochan's Shop", "Da Klass", "Carrefour Oasis Mall",
    "Carrefour Lugogo Mall", "Carrefour Arena Mall", "Carrefour Metroplex Mall",
    "Sankara Holaing International", "GJ Bar & Restaurant", "Sankara Enterprise",
    "Abujja Shopping Centre", "Serene Supermarket", "Delight Hospitality", "Mama Gideon",
    "Tango Shoppers", "Choma Zone Bugolobi", "Choma Zone Ntinda", "Shisha Nyama",
    "Ponus Restaurant", "Uncle's Choma", "PK Bar & Grill", "Rider's Lounge Acacia",
    "Capital Shoppers Ntinda", "Capital Shoppers Nakawa", "Capital Shoppers Nakasero",
    "Capital Shoppers Garden City", "Fraine Supermarket Kiira", "Fraine Supermarket Ntinda",
    "Fraine Supermarket Kiwatule", "Announceur Supermarket", "Masters Supermarket Ntinda",
    "Futureland Hypercash & Carry LTD", "Standard Supermarket Garden City",
    "Kenjoy Supermarket Najjanakumbi", "Kenjoy Supermrket Nansana", "Kenjoy Supermrket Mengo",
    "Fabulous Freeman", "Wilsen Supermarket", "Trolley's Supermarket", "Save Supermarket",
    "Greak Supermarket", "Cheapest Supermarket", "Global Centre aka Costco",
    "Carrefour Village Mall", "Carrefour Victoria Mall", "Best Buy Mengo", "Gold Supermarket",
    "Portbell Supermarket", "Liken Shoppers", "Rubymart Supermarket", "Kitoke",
    "Sitenda Supermarket", "Upland Supermarket", "Tazex Supermarket", "Padova Supermarket",
    "Prosale Enterprises", "Hajjati Lufuka", "Valley Enterprises", "Nvuma Shoppers",
    "T.D Wholesale", "J.K Supermarket", "Mukiga Shop", "Amos Wholesale", "Black Wholesale",
    "Penina's Shop", "Brenda Bayita Ababiri", "Masters Shop", "Olivia Enterprises",
    "Makoa Shoppers", "Umar Halal Wholesale",
]


class RecipientImportError(ValueError):
    pass


def _dedupe_within_batch(names):
    """
    Collapse case/whitespace-insensitive repeats WITHIN the supplied batch
    itself, keeping the first-encountered spelling of each. The comparison
    key (normalize_name: case-folded, leading/trailing/internal whitespace
    normalized) is used ONLY to detect duplicates — the name kept for
    `unique` (and ultimately stored) is the exact, untouched supplied
    string, whitespace and all. Never guess at what was "meant."
    """
    seen_normalized = set()
    unique = []
    in_batch_dupes = []
    for name in names:
        key = normalize_name(name)
        if key in seen_normalized:
            in_batch_dupes.append(name)
        else:
            seen_normalized.add(key)
            unique.append(name)
    return unique, in_batch_dupes


def _category_or_raise(category_name):
    category = SalesCategory.query.filter_by(name=category_name).first()
    if category is None:
        raise RecipientImportError(f"sales category '{category_name}' does not exist")
    return category


def _all_existing_by_normalized_name():
    """
    Every customer row, regardless of active/inactive/temporary/merged
    status — a name already in the table anywhere in its history must
    never be silently recreated.
    """
    return {c.normalized_name: c for c in Customer.query.all()}


def preview_batch(names, category_name):
    """
    Read-only — writes nothing to the customers table. Returns
    {to_create, exact_duplicates_skipped, possible_spelling_variations,
    in_batch_duplicates_collapsed}.
    """
    category = _category_or_raise(category_name)
    unique_names, in_batch_dupes = _dedupe_within_batch(names)

    existing_by_normalized = _all_existing_by_normalized_name()

    to_create = []
    exact_duplicates_skipped = []
    possible_spelling_variations = []

    for i, name in enumerate(unique_names):
        key = normalize_name(name)
        if key in existing_by_normalized:
            existing = existing_by_normalized[key]
            exact_duplicates_skipped.append({
                "name": name, "existing_customer_id": existing.id,
                "existing_customer_name": existing.name,
                "existing_sales_category_id": existing.sales_category_id,
                "existing_active": existing.active,
                "existing_is_temporary": existing.is_temporary,
                "existing_merged_into_id": existing.merged_into_id,
            })
            continue

        similar_to = [{"id": c.id, "name": c.name} for c in find_similar_customers(name)]
        # also compare against every OTHER name in this same batch — a big
        # bulk import is exactly where two typo'd variants of the same
        # customer are most likely to be sitting side by side.
        for j, other in enumerate(unique_names):
            if i == j or normalize_name(other) in existing_by_normalized:
                continue
            if difflib.SequenceMatcher(None, name.lower(), other.lower()).ratio() >= DUPLICATE_SIMILARITY_THRESHOLD:
                similar_to.append({"id": None, "name": other})

        if similar_to:
            possible_spelling_variations.append({"name": name, "similar_to": similar_to})
        to_create.append(name)

    return {
        "category": category.to_dict(),
        "total_supplied": len(names),
        "in_batch_duplicates_collapsed": in_batch_dupes,
        "to_create": to_create,
        "exact_duplicates_skipped": exact_duplicates_skipped,
        "possible_spelling_variations": possible_spelling_variations,
    }


def execute_batch(names, category_name, user):
    """
    Transactional: creates every name not already an exact
    (case/whitespace-insensitive) match against ANY existing customer row
    (active, inactive, temporary, or merged-away), in one commit-worthy
    unit of work. Re-running with the same input creates nothing new.

    Each insert runs in its own nested transaction (SAVEPOINT) as a second
    line of defense behind the in-Python pre-check: if the unique index on
    normalized_name rejects a row anyway (e.g. a genuine race with a
    concurrent import), that single name is skipped instead of the whole
    batch failing.
    """
    category = _category_or_raise(category_name)
    unique_names, in_batch_dupes = _dedupe_within_batch(names)
    existing_normalized = set(_all_existing_by_normalized_name().keys())

    created = []
    skipped = []
    for name in unique_names:
        key = normalize_name(name)
        if key in existing_normalized:
            skipped.append(name)
            continue

        try:
            with db.session.begin_nested():
                db.session.add(Customer(
                    name=name, active=True, is_temporary=False,
                    sales_category_id=category.id, created_by=user.id,
                ))
        except IntegrityError:
            skipped.append(name)
            continue

        created.append(name)
        existing_normalized.add(key)  # guards against a same-batch near-miss after dedupe

    db.session.flush()
    return {
        "category": category.to_dict(),
        "created_count": len(created),
        "created_names": created,
        "skipped_count": len(skipped),
        "skipped_names": skipped,
        "in_batch_duplicates_collapsed": in_batch_dupes,
    }
