"""All pipeline state in one SQLite database at paths.db_file().

Every fetched posting is stored, relevant or not, so the relevance filter can change
without a refetch. `relevant_query` is the SQL form of fetch._relevant; a test holds
the two to the same answer on every branch of the rule.
"""
from cairn.store.attachment_files import (
    add_attachment,
    attachment,
    attachments,
    delete_attachment,
)
from cairn.store.checklists import (
    add_item,
    checklist,
    checklist_item,
    delete_item,
    reorder,
    set_item,
)
from cairn.store.companies import (
    COMPANY_RETRY_DAYS,
    ICON_RETRY_DAYS,
    clear_icon_failures,
    companies_without_domain,
    companies_without_icon,
    company_domain,
    company_domains,
    count_companies_without_domain,
    icon_counts,
    icon_domains,
    logo,
    logos,
    save_company,
    save_logo,
)
from cairn.store.db import close, close_thread, connect, sqlite_library
from cairn.store.keys import (
    location_key,
    match_key,
    name_key,
    requisition,
    title_key,
    url_key,
    web_url,
)
from cairn.store.legacy import import_legacy
from cairn.store.postings import (
    get_posting,
    links_to_check,
    mark_inactive_missing,
    rename_sources,
    resolve,
    save_link_checks,
    upsert_postings,
)
from cairn.store.relevance import (
    active_postings_since,
    count_relevant,
    new_postings,
    relevant_query,
    sync_relevance,
)
from cairn.store.runs import (
    CALL_KINDS,
    call_counts,
    counts,
    finish_run,
    get_run,
    list_runs,
    record_call,
    relevant_ranked,
    set_log_end,
    start_run,
)
from cairn.store.schema import (
    DEFAULT_CHECKLIST,
    SCHEMA,
    SCHEMA_VERSION,
    SENT,
    STAGES,
    STATUSES,
)
from cairn.store.scores import (
    FEEDBACK_REASONS,
    clear_feedback,
    feedback,
    feedback_examples,
    feedback_stats,
    get_description,
    is_seen,
    mark_seen,
    save_description,
    save_reasons,
    save_scores,
    seen_ids,
    set_feedback,
    set_keywords,
)
from cairn.store.search import facets, search
from cairn.store.tracker import (
    add_stage,
    application,
    application_counts,
    application_events,
    applications,
    clear_application,
    delete_stage,
    get_stage,
    last_events,
    past_due_stages,
    set_note,
    set_status,
    stages_between,
    upcoming_stages,
    update_stage,
)
