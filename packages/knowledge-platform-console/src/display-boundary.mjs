const SENSITIVE_KEY_RE = /(?:password|passphrase|secret|token|api[_-]?key|access[_-]?key|refresh[_-]?token|authorization|auth[_-]?header|cookie|private[_-]?key|credential(?:[_-]?ref)?|connection[_-]?string|database[_-]?url|source[_-]?path|file[_-]?path|physical[_-]?path|raw[_-]?(?:markdown|content)|published[_-]?markdown|content[_-]?base64|(?:^|[_-])sql(?:[_-]|$))/i;
const SENSITIVE_NAME_RE = /[A-Za-z0-9_.-]*(?:password|passphrase|secret|token|api[_-]?key|access[_-]?key|refresh[_-]?token|authorization|auth[_-]?header|cookie|private[_-]?key|credential(?:[_-]?ref)?|connection[_-]?string|database[_-]?url)[A-Za-z0-9_.-]*/gi;
const SECRET_ASSIGNMENT_RE = /["']?[A-Za-z0-9_.-]*(?:password|passphrase|secret|api[_-]?key|access[_-]?key|refresh[_-]?token|authorization|auth[_-]?header|cookie|private[_-]?key|credential(?:[_-]?ref)?|connection[_-]?string|database[_-]?url)[A-Za-z0-9_.-]*["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;}]+)/gi;
const PATH_RE = /(?:file:\/\/|(?:^|[\s(=])\/(?:Users|private|tmp|var|home|etc|opt|usr|root|mnt|Applications|System|Volumes)\/[^\s)]+|(?:^|[\s(])~\/[^\s)]+|(?:^|[\s(=])(?:[A-Za-z]:[\\/]|\\\\)[^\s)]+)/gi;
const SQL_RE = /\b(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b[\s\S]{0,1600}/gi;

/** Make arbitrary server text safe for bounded Console display. */
export function safeDisplayText(value, maxLength = 2400) {
  if (typeof value !== "string") return "";
  try {
    const parsed = JSON.parse(value);
    if (parsed !== null && typeof parsed === "object") {
      return JSON.stringify(safeDisplayValue(parsed)).slice(0, maxLength);
    }
  } catch {
    // Treat non-JSON text with the bounded fallback rules below.
  }
  return value
    .replace(SECRET_ASSIGNMENT_RE, "[secret-redacted]")
    .replace(PATH_RE, "[local-reference]")
    .replace(SQL_RE, "[sql-redacted]")
    .replace(SENSITIVE_NAME_RE, "[sensitive-field]")
    .slice(0, maxLength);
}

/** Recursively bound and redact unknown response data before rendering it. */
export function safeDisplayValue(value, depth = 0) {
  if (depth > 4) return "[truncated]";
  if (typeof value === "string") return safeDisplayText(value);
  if (value === null || typeof value === "number" || typeof value === "boolean") return value;
  if (Array.isArray(value)) return value.slice(0, 100).map((item) => safeDisplayValue(item, depth + 1));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).slice(0, 100).map(([key, child], index) => [
      SENSITIVE_KEY_RE.test(key) ? `[sensitive-field-${index}]` : safeDisplayText(key, 160),
      SENSITIVE_KEY_RE.test(key) ? "[redacted]" : safeDisplayValue(child, depth + 1),
    ]));
  }
  return "[unsupported]";
}

export function formatDisplayError(response, fallback) {
  const code = typeof response?.error?.code === "string" ? response.error.code : "error";
  return `${safeDisplayText(code, 160)}: ${fallback}`;
}
