// Explicit, serialisable console state and pure transitions over it.

export function createState() {
  return {
    authenticated: false,
    traces: [],
    selectedTraceId: null,
    selectedTrace: null,
    selectedAttemptId: null,
    events: [],
    eventCursor: 0,
    expandedNodes: new Set(),
    filters: { status: "", before: null },
    polling: { active: false, stale: false, failures: 0 },
    simulator: { open: false, sessionId: null, submission: null, messages: [] },
  };
}

// Ignore sequences at or below the cursor, sort new ones, never duplicate.
export function mergeEvents(state, events) {
  const seen = new Set(state.events.map((event) => event.sequence));
  const fresh = (events || [])
    .filter((event) => event.sequence > state.eventCursor && !seen.has(event.sequence))
    .sort((a, b) => a.sequence - b.sequence);
  state.events = state.events.concat(fresh).sort((a, b) => a.sequence - b.sequence);
  if (state.events.length > 0) {
    state.eventCursor = state.events[state.events.length - 1].sequence;
  }
  return fresh;
}

export function toggleNode(state, nodeKey) {
  if (state.expandedNodes.has(nodeKey)) {
    state.expandedNodes.delete(nodeKey);
  } else {
    state.expandedNodes.add(nodeKey);
  }
  return state.expandedNodes.has(nodeKey);
}

// Selecting a trace resets the event window and opens failed nodes by default.
export function selectTrace(state, trace) {
  state.selectedTraceId = trace.id || trace.trace_id;
  state.selectedTrace = trace;
  state.events = [];
  state.eventCursor = 0;
  state.expandedNodes = new Set();
  for (const span of trace.spans || []) {
    if (span.status === "failed" || span.status === "cancelled") {
      state.expandedNodes.add(nodeKey(span));
    }
  }
  return state;
}

export function nodeKey(span) {
  return `${span.node || span.name}:${span.attempt || 1}`;
}

export function isTerminal(status) {
  return status === "succeeded" || status === "failed" || status === "completed";
}
