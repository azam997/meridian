// Feature gates for release scoping. Compile-time: flip and rebuild.

/** The general Submit Feedback entry points: the sidebar nav item and the
 *  pulls screen's "Report this" error button. Was hidden for the initial
 *  release; re-enabled now that v1.0 has shipped. The dashboard's
 *  over-ceiling anomaly nudge is live regardless of this flag (those
 *  reports are the data that improves the sim). */
export const GENERAL_FEEDBACK_ENTRY: boolean = true;
