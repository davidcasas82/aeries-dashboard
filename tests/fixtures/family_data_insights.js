import { timingSafeEqual } from "./auth.js";
import { readBody } from "./healthProbe.js";

// The mini's grade-insights runner is the only writer. Its Bearer is AERIES_INSIGHTS_SECRET,
// a secret that can write this one record and nothing else. PIN sessions only read it.
export const INSIGHTS_APP = "aeries-insights";
export const INSIGHTS_KEY = "latest";
const MAX_BYTES = 256 * 1024;
const MAX_STUDENTS = 8;
const MAX_STORIES = 40;
const MAX_TEXT = 600;
const MAX_CITES = 12;
const STUDENT_KEY = /^[A-Za-z0-9-]{1,32}$/;
const UPSERT = `INSERT INTO docs (app, key, payload, version, updated_at)
  VALUES (?, ?, ?, 1, ?)
  ON CONFLICT(app, key) DO UPDATE SET
    payload = excluded.payload, version = docs.version + 1, updated_at = excluded.updated_at`;

// Fails closed: an unset, empty or blank configured secret matches nothing, and a missing, empty
// or blank Bearer never reaches the compare. The compare itself is constant-time (timingSafeEqual
// walks the longer of the two strings and never returns early).
export function insightsSecretMatches(env, request) {
  const secret = typeof env.AERIES_INSIGHTS_SECRET === "string" ? env.AERIES_INSIGHTS_SECRET.trim() : "";
  if (!secret) return false;
  const match = (request.headers.get("Authorization") || "").match(/^Bearer[ \t]+(\S.*)$/i);
  const presented = match ? match[1].trim() : "";
  if (!presented) return false;
  return timingSafeEqual(presented, secret);
}

export async function putInsights(env, request, now = new Date()) {
  const body = await readBody(request, MAX_BYTES);
  if (body.tooLarge) return json(413, { error: "Those insights are too large." });
  let parsed;
  try {
    parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body.bytes));
  } catch {
    return json(400, { error: "Send JSON." });
  }
  const clean = cleanInsights(parsed);
  if (clean.error) return json(400, { error: clean.error });
  const updatedAt = now.toISOString();
  const doc = { students: clean.students, published_at: updatedAt };
  await env.DB.prepare(UPSERT).bind(INSIGHTS_APP, INSIGHTS_KEY, JSON.stringify(doc), updatedAt).run();
  return json(200, { ok: true, students: Object.keys(clean.students).length, updatedAt });
}

export async function getInsights(env) {
  const row = await env.DB.prepare(
    "SELECT payload, updated_at FROM docs WHERE app = ? AND key = ?"
  ).bind(INSIGHTS_APP, INSIGHTS_KEY).first();
  if (!row) return json(200, { insights: null, updatedAt: null });
  let doc = null;
  try {
    doc = JSON.parse(row.payload);
  } catch {
    doc = null;
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) doc = null;
  return json(200, { insights: doc, updatedAt: row.updated_at });
}

// Only the published fields are kept: per student, as_of, the headline text and cites, and each
// story's class, text and cites. Anything else in the body (drop reasons, slots) is not stored.
export function cleanInsights(body) {
  if (!isObject(body) || !isObject(body.students)) return { error: "Send { students: { ... } }." };
  const entries = Object.entries(body.students);
  if (entries.length > MAX_STUDENTS) return { error: "Too many students." };
  const students = {};
  for (const [key, up] of entries) {
    if (!STUDENT_KEY.test(key)) return { error: "Student keys must be short ids." };
    if (!isObject(up)) return { error: `students.${key} must be an object.` };
    const stories = up.stories == null ? [] : up.stories;
    if (!Array.isArray(stories) || stories.length > MAX_STORIES) return { error: `students.${key}.stories must be a short list.` };
    const out = { as_of: null, headline: null, stories: [] };
    if (up.as_of != null) {
      if (!isText(up.as_of, 40)) return { error: `students.${key}.as_of must be a short string.` };
      out.as_of = up.as_of;
    }
    if (up.headline != null) {
      const headline = cleanLine(up.headline, "text");
      if (!headline) return { error: `students.${key}.headline must have text.` };
      out.headline = headline;
    }
    for (const story of stories) {
      const line = cleanLine(story, "story");
      if (!line || !isText(story.class, 120)) return { error: `students.${key}.stories needs class and story text.` };
      out.stories.push({ class: story.class, story: line.story, cites: line.cites });
    }
    students[key] = out;
  }
  return { students };
}

function cleanLine(value, field) {
  if (!isObject(value) || !isText(value[field], MAX_TEXT)) return null;
  const cites = value.cites == null ? [] : value.cites;
  if (!Array.isArray(cites) || cites.length > MAX_CITES || !cites.every((c) => isText(c, 120))) return null;
  return { [field]: value[field], cites };
}

function isText(value, max) {
  return typeof value === "string" && value.trim() !== "" && value.length <= max;
}

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function json(status, data) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}
