// Google Classroom export for the family dashboard.
// Paste into an Apps Script project under the student's school account,
// together with tools/appsscript.json (explicit read-only scopes).
//
//   exportClassroom()  nightly: reads the student's own Classroom data and the
//                      Drive Classroom/ folder, writes classroom_export.json into
//                      a "Family dashboard" folder, and shares it with SHARE_WITH.
//                      Also shares the student's own Classroom files with the
//                      parent so they can be opened from the dashboard.
//   testClassroom()    probe: lists active courses and a few coursework titles.
//
// Set STUDENT_SLOT (1 = first student in the dashboard, 2 = second) and
// SHARE_WITH before running. The export never includes the student's name or
// number; the dashboard maps it by STUDENT_SLOT.

const STUDENT_SLOT = 1;
const SHARE_WITH = [
  'parent@example.com',
  'classroom-reader@family-classroom.iam.gserviceaccount.com',
];
const PARENT_EMAIL = SHARE_WITH[0];
const SCHOOL_YEAR_START = new Date('2026-08-01');
const EXPORT_FOLDER = 'Family dashboard';
const EXPORT_FILE = 'classroom_export.json';
const DOC_TEXT_LIMIT = 4000;
const DESCRIPTION_LIMIT = 4000;
const RECENT_DAYS = 120;
const ANNOUNCEMENTS_PER_COURSE = 5;
const DRIVE_DOC = 'application/vnd.google-apps.document';

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
  const me = Session.getActiveUser().getEmail();
  const notes = [];
  const since = new Date(Date.now() - RECENT_DAYS * 86400000);

  let courses = [];
  try {
    courses = listAll_(function (token) {
      return Classroom.Courses.list({ courseStates: ['ACTIVE'], pageSize: 50, pageToken: token });
    }, 'courses');
  } catch (e) {
    notes.push('courses.list failed: ' + e.message);
  }

  const exported = courses.map(function (course) {
    return exportCourse_(course, since, notes);
  });

  const driveFolders = indexDriveClassroom_(me);
  const payload = {
    export_version: 1,
    student_slot: STUDENT_SLOT,
    captured_at: new Date().toISOString(),
    source: courses.length ? 'classroom_apps_script' : 'classroom_drive',
    courses: exported,
    drive_folders: driveFolders,
    notes: notes,
  };

  const file = writeExport_(payload);
  const shareStats = shareOwnedFiles_(me);
  Logger.log('Exported ' + exported.length + ' courses, '
    + exported.reduce(function (n, c) { return n + c.items.length; }, 0) + ' items, '
    + driveFolders.length + ' Drive class folders. ' + shareStats
    + (notes.length ? ' Notes: ' + notes.join(' | ') : ''));
  return file.getUrl();
}

function exportCourse_(course, since, notes) {
  const courseId = course.id;
  const topics = {};
  try {
    listAll_(function (token) {
      return Classroom.Courses.Topics.list(courseId, { pageSize: 100, pageToken: token });
    }, 'topic').forEach(function (t) { topics[t.topicId] = t.name; });
  } catch (e) {
    notes.push('topics unavailable for a course: ' + e.message);
  }

  const submissions = {};
  try {
    listAll_(function (token) {
      return Classroom.Courses.CourseWork.StudentSubmissions.list(courseId, '-', {
        userId: 'me', pageSize: 100, pageToken: token,
      });
    }, 'studentSubmissions').forEach(function (s) { submissions[s.courseWorkId] = s; });
  } catch (e) {
    notes.push('submissions unavailable for a course: ' + e.message);
  }

  const items = [];
  try {
    listAll_(function (token) {
      return Classroom.Courses.CourseWork.list(courseId, { pageSize: 100, pageToken: token });
    }, 'courseWork').forEach(function (w) {
      if (!isRecent_(w, since)) return;
      items.push(courseWorkItem_(w, submissions[w.id], topics));
    });
  } catch (e) {
    notes.push('courseWork unavailable for a course: ' + e.message);
  }

  try {
    listAll_(function (token) {
      return Classroom.Courses.CourseWorkMaterials.list(courseId, { pageSize: 100, pageToken: token });
    }, 'courseWorkMaterial').forEach(function (m) {
      if (!isRecent_(m, since)) return;
      items.push({
        id: m.id,
        type: 'material',
        title: m.title || '',
        description: clip_(m.description, DESCRIPTION_LIMIT),
        link: m.alternateLink || '',
        topic: topics[m.topicId] || '',
        assigned_at: m.creationTime || '',
        updated_at: m.updateTime || '',
        materials: materials_(m.materials),
      });
    });
  } catch (e) {
    notes.push('materials unavailable for a course: ' + e.message);
  }

  try {
    const res = Classroom.Courses.Announcements.list(courseId, {
      pageSize: ANNOUNCEMENTS_PER_COURSE, orderBy: 'updateTime desc',
    });
    (res.announcements || []).forEach(function (a) {
      items.push({
        id: a.id,
        type: 'announcement',
        title: '',
        description: clip_(a.text, 1500),
        link: a.alternateLink || '',
        assigned_at: a.creationTime || '',
        updated_at: a.updateTime || '',
        materials: materials_(a.materials, true),
      });
    });
  } catch (e) {
    notes.push('announcements unavailable for a course: ' + e.message);
  }

  return {
    id: courseId,
    name: course.name || '',
    section: course.section || '',
    room: course.room || '',
    link: course.alternateLink || '',
    items: items,
  };
}

function courseWorkItem_(w, sub, topics) {
  const item = {
    id: w.id,
    type: workType_(w.workType),
    title: w.title || '',
    description: clip_(w.description, DESCRIPTION_LIMIT),
    link: w.alternateLink || '',
    topic: topics[w.topicId] || '',
    assigned_at: w.creationTime || '',
    updated_at: w.updateTime || '',
    due: dueIso_(w.dueDate, w.dueTime),
    max_points: w.maxPoints == null ? null : w.maxPoints,
    materials: materials_(w.materials),
  };
  if (sub) {
    item.submission = {
      state: sub.state || '',
      late: !!sub.late,
      turned_in_at: turnedInAt_(sub),
      assigned_grade: sub.assignedGrade == null ? null : sub.assignedGrade,
      link: sub.alternateLink || '',
      attachments: [],
    };
    const atts = (sub.assignmentSubmission && sub.assignmentSubmission.attachments) || [];
    atts.forEach(function (a) {
      if (a.driveFile) {
        item.submission.attachments.push(driveAttachment_(a.driveFile, w.title, true));
      } else if (a.link) {
        item.submission.attachments.push({ kind: 'link', title: a.link.title || '', url: a.link.url || '' });
      }
    });
  }
  return item;
}

function materials_(list, skipText) {
  const out = [];
  (list || []).forEach(function (m) {
    if (m.driveFile && m.driveFile.driveFile) {
      out.push(driveAttachment_(m.driveFile.driveFile, '', !skipText));
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

function driveAttachment_(df, courseWorkTitle, withText) {
  const att = {
    kind: 'drive',
    id: df.id || '',
    title: stripStudentPrefix_(df.title || '', courseWorkTitle),
    url: df.alternateLink || '',
    mime: '',
  };
  if (!df.id) return att;
  try {
    att.mime = DriveApp.getFileById(df.id).getMimeType();
    if (withText && att.mime === DRIVE_DOC) {
      att.text_excerpt = clip_(DocumentApp.openById(df.id).getBody().getText(), DOC_TEXT_LIMIT);
    }
  } catch (e) {
    att.note = 'not readable';
  }
  return att;
}

function indexDriveClassroom_(me) {
  const folders = [];
  const roots = DriveApp.getFoldersByName('Classroom');
  while (roots.hasNext()) {
    const classFolders = roots.next().getFolders();
    while (classFolders.hasNext()) {
      const folder = classFolders.next();
      if (folder.getDateCreated() < SCHOOL_YEAR_START) continue;
      const files = [];
      const it = folder.getFiles();
      while (it.hasNext()) {
        const f = it.next();
        let target = f;
        let readable = true;
        try {
          if (f.getMimeType() === 'application/vnd.google-apps.shortcut') {
            target = DriveApp.getFileById(f.getTargetId());
          }
        } catch (e) {
          readable = false;
        }
        let owned = false;
        try { owned = readable && target.getOwner().getEmail() === me; } catch (e) { owned = false; }
        files.push({
          id: readable ? target.getId() : '',
          title: stripStudentPrefix_(f.getName(), ''),
          mime: readable ? target.getMimeType() : '',
          created_at: f.getDateCreated().toISOString(),
          modified_at: readable ? target.getLastUpdated().toISOString() : '',
          owned_by_student: owned,
        });
      }
      folders.push({ name: folder.getName(), created_at: folder.getDateCreated().toISOString(), files: files });
    }
  }
  return folders;
}

function shareOwnedFiles_(me) {
  let shared = 0, already = 0, skipped = 0, failed = 0;
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
          const target = f.getMimeType() === 'application/vnd.google-apps.shortcut'
            ? DriveApp.getFileById(f.getTargetId())
            : f;
          // Only the student's own files. Group docs and teacher materials carry
          // other people's work and must never be shared outside the domain.
          if (target.getOwner().getEmail() !== me) { skipped++; continue; }
          const people = target.getViewers().concat(target.getEditors()).map(function (u) { return u.getEmail(); });
          if (people.indexOf(PARENT_EMAIL) >= 0) { already++; continue; }
          target.addViewer(PARENT_EMAIL);
          shared++;
        } catch (e) {
          failed++;
        }
      }
    }
  }
  return 'Shared ' + shared + ' new files with the parent, ' + already + ' already shared, '
    + skipped + ' not owned by student (skipped), ' + failed + ' could not be shared.';
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
