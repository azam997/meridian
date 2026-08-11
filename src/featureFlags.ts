// Feature gates for release scoping. Compile-time: flip and rebuild.

/** The general Submit Feedback entry points: the sidebar nav item and the
 *  pulls screen's "Report this" error button. Hidden for the initial release
 *  so routine reports can't flood the GitHub issue tracker. The dashboard's
 *  over-ceiling anomaly nudge stays live regardless of this flag (those
 *  reports are the data that improves the sim), and FeedbackView remains
 *  reachable through it. */
export const GENERAL_FEEDBACK_ENTRY: boolean = false;
