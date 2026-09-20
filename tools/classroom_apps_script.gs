// Google Classroom export for the family dashboard. Script v3 (frozen).
//
// This script is a DUMP ONLY. It reads everything the student can see about
// their own Classroom work and writes it, unprocessed, into one JSON file.
// Matching to Aeries, text cleanup, briefings, study guides and tutoring all
// happen in the aeries-dashboard repo (classroom.py) or the family-data
// Worker. The student should only need to paste this file again if Google
// changes the Classroom API itself or a data surface that is not exported
// here is wanted. Product changes never require a re-paste.
//
// Paste into an Apps Script project under the student's school account,
// together with tools/appsscript.json (explicit read-only scopes).
//
//   exportClassroom()  nightly: reads courses, teachers, topics, coursework,
//                      rubrics, materials, announcements, the student's own
//                      submissions (with history), attachment file ids and
//                      Doc/Slides text, plus an index of the Drive Classroom/
//                      folder. Writes classroom_export.json into a
//                      "Family dashboard" folder and shares it with SHARE_WITH.
//                      Also shares the student's own Classroom files with
//                      SHARE_WITH so the dashboard can read them later.
//   testClassroom()    probe: lists active courses and a few coursework titles.
//
// Set STUDENT_SLOT (1 = first student in the dashboard, 2 = second) and
// SHARE_WITH before running. The export never includes the student's name,
// email or number; the dashboard maps it by STUDENT_SLOT. Nothing here lists
// other students or shares files the student does not own.
//
// Every extra API call soft-fails: if a permission is missing or an endpoint
// is blocked, the run continues and the gap is recorded in payload.notes.

const STUDENT_SLOT = 1;
const SHARE_WITH = [
  'parent@example.com',
  'classroom-reader@family-classroom.iam.gserviceaccount.com',
];
const SCRIPT_VERSION = '3.1';
const EXPORT_VERSION = 2;
const SCHOOL_YEAR_START = new Date('2026-08-01');
const EXPORT_FOLDER = 'Family dashboard';
const EXPORT_FILE = 'classroom_export.json';
const DOC_TEXT_LIMIT = 6000;
const DESCRIPTION_LIMIT = 6000;
const ANNOUNCEMENT_LIMIT = 3000;
const RECENT_DAYS = 400;               // whole school year; the dashboard windows it down
const TEXT_TIME_BUDGET_MS = 4 * 60000; // stop reading Doc text after 4 minutes, index the rest
const INCLUDE_STUDENT_WORK_TEXT = true; // text of the student's own submitted Docs

// Google Docs and Slides export to plain text through the Drive API, which the
// existing Drive permission already covers (no Docs permission needed).
const TEXT_EXPORTS = {
  'application/vnd.google-apps.document': 'text/plain',
  'application/vnd.google-apps.presentation': 'text/plain',
};
const SHORTCUT_MIME = 'application/vnd.google-apps.shortcut';

function testClassroom() {
  const courses = listAll_(function (token) {
    return Classroom.Courses.list({ courseStates: ['ACTIVE'], pageSize: 50, pageToken: token });
  }, 'courses');
  Logger.log(courses.length + ' active courses');
  courses.forEach(function (c) { Logger.log('- ' + c.name); });
  if (courses.length) {
    const work = Classroom.Courses.CourseWork.list(courses[0].id, { pageSize: 3 });
    const titles = (work.courseWork || []).map(function (w) { return w.title; });
    Logger.log('Sample coursework in "' + courses[0].name + '": ' + JSON.stringify(titles));
  }
}

function exportClassroom() {
  const run = {
    me: Session.getActiveUser().getEmail(),
    startedAt: Date.now(),
    notes: [],
    since: new Date(Date.now() - RECENT_DAYS * 86400000),
    cache: loadPreviousText_(),
    text: { total: 0, withText: 0, reused: 0, failed: 0, skippedTime: 0, firstError: '' },
    fileMeta: {},
  };

  let courses = [];
  try {
    courses = listAll_(function (token) {
      return Classroom.Courses.list({ courseStates: ['ACTIVE'], pageSize: 50, pageToken: token });
    }, 'courses');
  } catch (e) {
    run.notes.push('courses.list failed: ' + e.message);
  }

  const exported = courses.map(function (course) {
    return exportCourse_(course, run);
  });

  const driveFolders = indexDriveClassroom_(run);
  const payload = {
    export_version: EXPORT_VERSION,
    script_version: SCRIPT_VERSION,
    student_slot: STUDENT_SLOT,
    captured_at: new Date().toISOString(),
    time_zone: Session.getScriptTimeZone(),
    source: courses.length ? 'classroom_apps_script' : 'classroom_drive',
    recent_days: RECENT_DAYS,
    courses: exported,
    drive_folders: driveFolders,
    doc_text: {
      total: run.text.total,
      with_text: run.text.withText,
      reused: run.text.reused,
      failed: run.text.failed,
      skipped_time_budget: run.text.skippedTime,
      first_error: run.text.firstError,
    },
    notes: run.notes,
    elapsed_ms: Date.now() - run.startedAt,
  };

  const file = writeExport_(payload);
  const shareStats = shareOwnedFiles_(run);
  const t = run.text;
  Logger.log('Exported ' + exported.length + ' courses, '
    + exported.reduce(function (n, c) { return n + c.items.length; }, 0) + ' items, '
    + driveFolders.length + ' Drive class folders. Doc text: ' + t.withText + ' of '
    + t.total + ' files read (' + t.reused + ' reused'
    + (t.skippedTime ? ', ' + t.skippedTime + ' skipped for time' : '')
    + (t.failed ? ', ' + t.failed + ' not readable: ' + t.firstError : '') + '). '
    + shareStats
    + (run.notes.length ? ' Notes: ' + run.notes.join(' | ') : ''));
  return file.getUrl();
}

// ---------------------------------------------------------------- courses

function exportCourse_(course, run) {
  const courseId = course.id;
  const out = {
    id: courseId,
    name: course.name || '',
    section: course.section || '',
    description_heading: course.descriptionHeading || '',
    description: clip_(course.description, 2000),
    room: course.room || '',
    link: course.alternateLink || '',
    state: course.courseState || '',
    owner_id: course.ownerId || '',
    created_at: course.creationTime || '',
    updated_at: course.updateTime || '',
    calendar_id: course.calendarId || '',
    teachers: [],
    topics: [],
    items: [],
  };

  // Teachers only. Students.list is never called.
  try {
    out.teachers = listAll_(function (token) {
      return Classroom.Courses.Teachers.list(courseId, { pageSize: 50, pageToken: token });
    }, 'teachers').map(function (t) {
      const p = t.profile || {};
      return {
        id: t.userId || p.id || '',
        name: (p.name && p.name.fullName) || '',
        is_owner: !!course.ownerId && (t.userId === course.ownerId || p.id === course.ownerId),
      };
    });
  } catch (e) {
    run.notes.push('teachers unavailable for a course: ' + e.message);
  }

  const topics = {};
  try {
    listAll_(function (token) {
      return Classroom.Courses.Topics.list(courseId, { pageSize: 100, pageToken: token });
    }, 'topic').forEach(function (t) {
      topics[t.topicId] = t.name;
      out.topics.push({ id: t.topicId, name: t.name || '', updated_at: t.updateTime || '' });
    });
  } catch (e) {
    run.notes.push('topics unavailable for a course: ' + e.message);
  }

  const submissions = {};
  try {
    listAll_(function (token) {
      return Classroom.Courses.CourseWork.StudentSubmissions.list(courseId, '-', {
        userId: 'me', pageSize: 100, pageToken: token,
      });
    }, 'studentSubmissions').forEach(function (s) { submissions[s.courseWorkId] = s; });
  } catch (e) {
    run.notes.push('submissions unavailable for a course: ' + e.message);
  }

  try {
    listAll_(function (token) {
      return Classroom.Courses.CourseWork.list(courseId, { pageSize: 100, pageToken: token });
    }, 'courseWork').forEach(function (w) {
      if (!isRecent_(w, run.since)) return;
      const item = courseWorkItem_(w, submissions[w.id], topics, run);
      const rubric = rubricFor_(courseId, w.id, run);
      if (rubric) item.rubric = rubric;
      out.items.push(item);
    });
  } catch (e) {
    run.notes.push('courseWork unavailable for a course: ' + e.message);
  }

  try {
    listAll_(function (token) {
      return Classroom.Courses.CourseWorkMaterials.list(courseId, { pageSize: 100, pageToken: token });
    }, 'courseWorkMaterial').forEach(function (m) {
      if (!isRecent_(m, run.since)) return;
      out.items.push({
        id: m.id,
        type: 'material',
        title: m.title || '',
        description: clip_(m.description, DESCRIPTION_LIMIT),
        link: m.alternateLink || '',
        topic: topics[m.topicId] || '',
        topic_id: m.topicId || '',
        state: m.state || '',
        assignee_mode: m.assigneeMode || '',
        assigned_at: m.creationTime || '',
        updated_at: m.updateTime || '',
        scheduled_at: m.scheduledTime || '',
        materials: materials_(m.materials, run, true),
      });
    });
  } catch (e) {
    run.notes.push('materials unavailable for a course: ' + e.message);
  }

  try {
    listAll_(function (token) {
      return Classroom.Courses.Announcements.list(courseId, {
        pageSize: 100, orderBy: 'updateTime desc', pageToken: token,
      });
    }, 'announcements').forEach(function (a) {
      if (!isRecent_(a, run.since)) return;
      out.items.push({
        id: a.id,
        type: 'announcement',
        title: '',
        description: clip_(a.text, ANNOUNCEMENT_LIMIT),
        link: a.alternateLink || '',
        state: a.state || '',
        assignee_mode: a.assigneeMode || '',
        assigned_at: a.creationTime || '',
        updated_at: a.updateTime || '',
        scheduled_at: a.scheduledTime || '',
        materials: materials_(a.materials, run, false),
      });
    });
  } catch (e) {
    run.notes.push('announcements unavailable for a course: ' + e.message);
  }

  return out;
}

function courseWorkItem_(w, sub, topics, run) {
  const item = {
    id: w.id,
    type: workType_(w.workType),
    work_type: w.workType || '',
    title: w.title || '',
    description: clip_(w.description, DESCRIPTION_LIMIT),
    link: w.alternateLink || '',
    topic: topics[w.topicId] || '',
    topic_id: w.topicId || '',
    state: w.state || '',
    assignee_mode: w.assigneeMode || '',
    assigned_at: w.creationTime || '',
    updated_at: w.updateTime || '',
    scheduled_at: w.scheduledTime || '',
    due: dueIso_(w.dueDate, w.dueTime),
    due_raw: w.dueDate ? { date: w.dueDate, time: w.dueTime || null } : null,
    max_points: w.maxPoints == null ? null : w.maxPoints,
    grade_category: w.gradeCategory ? {
      id: w.gradeCategory.id || '',
      name: w.gradeCategory.name || '',
      weight: w.gradeCategory.weight == null ? null : w.gradeCategory.weight,
      default_denominator: w.gradeCategory.defaultGradeDenominator == null
        ? null : w.gradeCategory.defaultGradeDenominator,
    } : null,
    submission_modification_mode: w.submissionModificationMode || '',
    choices: (w.multipleChoiceQuestion && w.multipleChoiceQuestion.choices) || [],
    materials: materials_(w.materials, run, true),
  };
  if (sub) item.submission = submission_(sub, w.title, run);
  return item;
}

function submission_(sub, courseWorkTitle, run) {
  const s = {
    id: sub.id || '',
    state: sub.state || '',
    late: !!sub.late,
    turned_in_at: turnedInAt_(sub),
    assigned_grade: sub.assignedGrade == null ? null : sub.assignedGrade,
    draft_grade: sub.draftGrade == null ? null : sub.draftGrade,
    course_work_type: sub.courseWorkType || '',
    link: sub.alternateLink || '',
    created_at: sub.creationTime || '',
    updated_at: sub.updateTime || '',
    answer: null,
    history: [],
    attachments: [],
  };
  if (sub.shortAnswerSubmission && sub.shortAnswerSubmission.answer != null) {
    s.answer = clip_(sub.shortAnswerSubmission.answer, 2000);
  } else if (sub.multipleChoiceSubmission && sub.multipleChoiceSubmission.answer != null) {
    s.answer = sub.multipleChoiceSubmission.answer;
  }
  (sub.submissionHistory || []).forEach(function (h) {
    if (h.stateHistory) {
      s.history.push({ kind: 'state', state: h.stateHistory.state || '', at: h.stateHistory.stateTimestamp || '' });
    } else if (h.gradeHistory) {
      const g = h.gradeHistory;
      s.history.push({
        kind: 'grade',
        points_earned: g.pointsEarned == null ? null : g.pointsEarned,
        max_points: g.maxPoints == null ? null : g.maxPoints,
        change: g.gradeChangeType || '',
        at: g.gradeTimestamp || '',
      });
    }
  });
  const atts = (sub.assignmentSubmission && sub.assignmentSubmission.attachments) || [];
  atts.forEach(function (a) {
    if (a.driveFile) {
      s.attachments.push(driveAttachment_(a.driveFile, courseWorkTitle, INCLUDE_STUDENT_WORK_TEXT, run, ''));
    } else if (a.link) {
      s.attachments.push({ kind: 'link', title: a.link.title || '', url: a.link.url || '' });
    } else if (a.youTubeVideo) {
      s.attachments.push({ kind: 'youtube', title: a.youTubeVideo.title || '', url: a.youTubeVideo.alternateLink || '' });
    } else if (a.form) {
      s.attachments.push({ kind: 'form', title: a.form.title || '', url: a.form.formUrl || '' });
    }
  });
  return s;
}

// Rubrics are read per coursework item. A permission-style failure turns the
// lookup off for the rest of the run so it costs one note, not one per item.
function rubricFor_(courseId, courseWorkId, run) {
  if (run.rubricsOff) return null;
  const api = Classroom.Courses.CourseWork && Classroom.Courses.CourseWork.Rubrics;
  if (!api || typeof api.list !== 'function') {
    run.rubricsOff = true;
    run.notes.push('rubrics: not exposed by the Classroom service');
    return null;
  }
  try {
    const res = api.list(courseId, courseWorkId) || {};
    const rubric = (res.rubrics || [])[0];
    return rubric ? rubric_(rubric) : null;
  } catch (e) {
    if (/permission|403|insufficient|scope|authoriz/i.test(e.message || '')) {
      run.rubricsOff = true;
      run.notes.push('rubrics unavailable: ' + e.message);
    }
    return null;
  }
}

function rubric_(r) {
  return {
    id: r.id || '',
    source_spreadsheet_id: r.sourceSpreadsheetId || '',
    created_at: r.creationTime || '',
    updated_at: r.updateTime || '',
    criteria: (r.criteria || []).map(function (c) {
      return {
        id: c.id || '',
        title: c.title || '',
        description: clip_(c.description, 1000),
        levels: (c.levels || []).map(function (l) {
          return {
            id: l.id || '',
            title: l.title || '',
            description: clip_(l.description, 600),
            points: l.points == null ? null : l.points,
          };
        }),
      };
    }),
  };
}

// ---------------------------------------------------------------- attachments

function materials_(list, run, withText) {
  const out = [];
  (list || []).forEach(function (m) {
    if (m.driveFile && m.driveFile.driveFile) {
      out.push(driveAttachment_(m.driveFile.driveFile, '', withText, run, m.driveFile.shareMode || ''));
    } else if (m.link) {
      out.push({ kind: 'link', title: m.link.title || '', url: m.link.url || '' });
    } else if (m.youtubeVideo) {
      out.push({ kind: 'youtube', title: m.youtubeVideo.title || '', url: m.youtubeVideo.alternateLink || '' });
    } else if (m.form) {
      out.push({ kind: 'form', title: m.form.title || '', url: m.form.formUrl || '' });
    }
  });
  return out;
}

function driveAttachment_(df, courseWorkTitle, withText, run, shareMode) {
  const att = {
    kind: 'drive',
    id: df.id || '',
    title: stripStudentPrefix_(df.title || '', courseWorkTitle),
    url: df.alternateLink || '',
    mime: '',
    share_mode: shareMode || '',
    modified_at: '',
    owned_by_student: false,
  };
  if (!df.id) return att;
  const meta = fileMeta_(df.id, run);
  if (!meta.ok) {
    att.note = 'file not accessible: ' + meta.error;
    return att;
  }
  att.mime = meta.mime;
  att.modified_at = meta.modifiedAt;
  att.owned_by_student = meta.owned;
  if (!withText || !TEXT_EXPORTS[att.mime]) return att;

  run.text.total++;
  const cached = run.cache[df.id];
  if (cached && cached.modified_at === att.modified_at && cached.text_excerpt) {
    att.text_excerpt = cached.text_excerpt;
    run.text.withText++;
    run.text.reused++;
    return att;
  }
  if (Date.now() - run.startedAt > TEXT_TIME_BUDGET_MS) {
    run.text.skippedTime++;
    att.note = 'text skipped: time budget';
    return att;
  }
  try {
    att.text_excerpt = clip_(exportText_(df.id, att.mime), DOC_TEXT_LIMIT);
    if (att.text_excerpt) run.text.withText++;
  } catch (e) {
    run.text.failed++;
    att.note = 'text not readable: ' + e.message;
    if (!run.text.firstError) run.text.firstError = e.message;
  }
  return att;
}

// One DriveApp lookup per file id per run; the same Doc is often attached to
// several items.
function fileMeta_(id, run) {
  if (run.fileMeta[id]) return run.fileMeta[id];
  let meta;
  try {
    const f = DriveApp.getFileById(id);
    let owned = false;
    try { owned = f.getOwner().getEmail() === run.me; } catch (e) { owned = false; }
    meta = {
      ok: true,
      mime: f.getMimeType(),
      modifiedAt: f.getLastUpdated().toISOString(),
      owned: owned,
      file: f,
    };
  } catch (e) {
    meta = { ok: false, error: e.message };
  }
  run.fileMeta[id] = meta;
  return meta;
}

// alt=media is required or the Drive service returns only metadata
// ("Export requires alt=media to download the exported content").
function exportText_(id, mime) {
  const res = Drive.Files.export(id, TEXT_EXPORTS[mime], { alt: 'media' });
  if (typeof res === 'string') return res;
  if (res && typeof res.getDataAsString === 'function') return res.getDataAsString();
  if (res && typeof res.getBlob === 'function') return res.getBlob().getDataAsString();
  if (res && typeof res.getContentText === 'function') return res.getContentText();
  return String(res || '');
}

// Text read on the previous night is reused when the file has not changed, so
// the nightly run stays well inside the time budget.
function loadPreviousText_() {
  const cache = {};
  try {
    const root = DriveApp.getRootFolder();
    const folders = root.getFoldersByName(EXPORT_FOLDER);
    if (!folders.hasNext()) return cache;
    const files = folders.next().getFilesByName(EXPORT_FILE);
    if (!files.hasNext()) return cache;
    const prev = JSON.parse(files.next().getBlob().getDataAsString());
    (prev.courses || []).forEach(function (c) {
      (c.items || []).forEach(function (item) {
        const mats = (item.materials || []).concat((item.submission && item.submission.attachments) || []);
        mats.forEach(function (m) {
          if (m.kind === 'drive' && m.id && m.text_excerpt && m.modified_at) {
            cache[m.id] = { modified_at: m.modified_at, text_excerpt: m.text_excerpt };
          }
        });
      });
    });
  } catch (e) {
    // No previous export or unreadable: start cold.
  }
  return cache;
}

// ---------------------------------------------------------------- Drive index

function indexDriveClassroom_(run) {
  const folders = [];
  const roots = DriveApp.getFoldersByName('Classroom');
  while (roots.hasNext()) {
    const classFolders = roots.next().getFolders();
    while (classFolders.hasNext()) {
      const folder = classFolders.next();
      const files = [];
      const it = folder.getFiles();
      while (it.hasNext()) {
        const f = it.next();
        let target = f;
        let readable = true;
        let isShortcut = false;
        try {
          if (f.getMimeType() === SHORTCUT_MIME) {
            isShortcut = true;
            target = DriveApp.getFileById(f.getTargetId());
          }
        } catch (e) {
          readable = false;
        }
        let owned = false;
        try { owned = readable && target.getOwner().getEmail() === run.me; } catch (e) { owned = false; }
        let size = null;
        try { size = readable ? target.getSize() : null; } catch (e) { size = null; }
        files.push({
          id: readable ? target.getId() : '',
          shortcut_id: isShortcut ? f.getId() : '',
          title: stripStudentPrefix_(f.getName(), ''),
          mime: readable ? target.getMimeType() : '',
          url: readable ? target.getUrl() : '',
          size: size,
          created_at: f.getDateCreated().toISOString(),
          modified_at: readable ? target.getLastUpdated().toISOString() : '',
          owned_by_student: owned,
          readable: readable,
        });
      }
      folders.push({
        id: folder.getId(),
        name: folder.getName(),
        url: folder.getUrl(),
        created_at: folder.getDateCreated().toISOString(),
        current_year: folder.getDateCreated() >= SCHOOL_YEAR_START,
        files: files,
      });
    }
  }
  return folders;
}

// ---------------------------------------------------------------- sharing

// Only the student's own files, only from this school year's class folders and
// this year's coursework. Group docs and teacher materials carry other people's
// work and must never be shared outside the domain.
function shareOwnedFiles_(run) {
  const recipients = SHARE_WITH.filter(function (e) { return e && e.indexOf('@example.com') < 0; });
  let shared = 0, already = 0, skipped = 0, failed = 0;
  const seen = {};

  function shareTarget_(target) {
    const id = target.getId();
    if (seen[id]) return;
    seen[id] = true;
    let ownerEmail = '';
    try { ownerEmail = target.getOwner().getEmail(); } catch (e) { ownerEmail = ''; }
    if (ownerEmail !== run.me) { skipped++; return; }
    const people = target.getViewers().concat(target.getEditors()).map(function (u) { return u.getEmail(); });
    let added = false;
    recipients.forEach(function (email) {
      if (people.indexOf(email) >= 0) return;
      try { target.addViewer(email); added = true; } catch (e) { failed++; }
    });
    if (added) shared++; else already++;
  }

  const roots = DriveApp.getFoldersByName('Classroom');
  while (roots.hasNext()) {
    const classFolders = roots.next().getFolders();
    while (classFolders.hasNext()) {
      const folder = classFolders.next();
      if (folder.getDateCreated() < SCHOOL_YEAR_START) continue;
      const files = folder.getFiles();
      while (files.hasNext()) {
        const f = files.next();
        try {
          const target = f.getMimeType() === SHORTCUT_MIME ? DriveApp.getFileById(f.getTargetId()) : f;
          shareTarget_(target);
        } catch (e) {
          failed++;
        }
      }
    }
  }
  // Student-owned files attached to coursework this run (submissions), even
  // when they live outside the Classroom/ folder.
  Object.keys(run.fileMeta).forEach(function (id) {
    const meta = run.fileMeta[id];
    if (!meta.ok || !meta.owned || !meta.file) return;
    try { shareTarget_(meta.file); } catch (e) { failed++; }
  });
  return 'Shared ' + shared + ' student-owned files (' + already + ' already shared, '
    + skipped + ' not owned by student, skipped; ' + failed + ' could not be shared).';
}

function writeExport_(payload) {
  const root = DriveApp.getRootFolder();
  const folders = root.getFoldersByName(EXPORT_FOLDER);
  const folder = folders.hasNext() ? folders.next() : root.createFolder(EXPORT_FOLDER);
  const json = JSON.stringify(payload);
  const existing = folder.getFilesByName(EXPORT_FILE);
  const file = existing.hasNext()
    ? existing.next().setContent(json)
    : folder.createFile(EXPORT_FILE, json, 'application/json');
  const people = file.getViewers().concat(file.getEditors()).map(function (u) { return u.getEmail(); });
  SHARE_WITH.forEach(function (email) {
    if (!email || email.indexOf('@example.com') >= 0 || people.indexOf(email) >= 0) return;
    try { file.addViewer(email); } catch (e) { Logger.log('Could not share export with one address: ' + e.message); }
  });
  return file;
}

// ---------------------------------------------------------------- helpers

function listAll_(fetchPage, key) {
  const out = [];
  let token = null;
  do {
    const res = fetchPage(token) || {};
    (res[key] || []).forEach(function (x) { out.push(x); });
    token = res.nextPageToken || null;
  } while (token);
  return out;
}

function isRecent_(w, since) {
  const stamp = w.updateTime || w.creationTime;
  if (!stamp) return true;
  if (new Date(stamp) >= since) return true;
  if (w.dueDate) {
    const due = new Date(Date.UTC(w.dueDate.year, (w.dueDate.month || 1) - 1, w.dueDate.day || 1));
    return due >= since;
  }
  return false;
}

function dueIso_(dueDate, dueTime) {
  if (!dueDate || !dueDate.year) return '';
  const h = dueTime && dueTime.hours != null ? dueTime.hours : null;
  const m = dueTime && dueTime.minutes != null ? dueTime.minutes : 0;
  if (h == null) return pad_(dueDate.year, 4) + '-' + pad_(dueDate.month, 2) + '-' + pad_(dueDate.day, 2);
  return new Date(Date.UTC(dueDate.year, dueDate.month - 1, dueDate.day, h, m)).toISOString();
}

function turnedInAt_(sub) {
  let latest = '';
  (sub.submissionHistory || []).forEach(function (h) {
    const s = h.stateHistory;
    if (s && (s.state === 'TURNED_IN' || s.state === 'RETURNED') && s.stateTimestamp > latest) {
      latest = s.stateTimestamp;
    }
  });
  return latest;
}

function workType_(t) {
  if (t === 'SHORT_ANSWER_QUESTION' || t === 'MULTIPLE_CHOICE_QUESTION') return 'question';
  return 'assignment';
}

// Classroom names per-student copies "<Student Name> - <Title>". Drop the name.
function stripStudentPrefix_(title, courseWorkTitle) {
  if (!title) return '';
  if (courseWorkTitle && title.length > courseWorkTitle.length
      && title.slice(-courseWorkTitle.length) === courseWorkTitle) {
    return courseWorkTitle;
  }
  const m = title.match(/^([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){1,3})\s+-\s+(.+)$/);
  return m ? m[2] : title;
}

function clip_(s, n) {
  s = (s || '').toString();
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function pad_(n, w) {
  let s = String(n);
  while (s.length < w) s = '0' + s;
  return s;
}
