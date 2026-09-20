// Paste into an Apps Script project under the student's school account.
// Phase 0 helpers from docs/proactive-study-plan.md:
//   testClassroom()            probes whether the Classroom API scope is allowed
//   shareClassroomWithParent() follows the shortcuts in Drive's Classroom/ folder
//                              and adds the parent as viewer on each real file.
// Set PARENT_EMAIL before running. Run shareClassroomWithParent on a nightly
// time-driven trigger so new assignments become readable without a click.
// Pair with tools/appsscript.json: its explicit oauthScopes keep the
// permission prompt to read-only Classroom (own classes and coursework),
// Drive (needed to add a viewer), and the account email. Without it, the
// Classroom advanced service requests roster and class-management scopes.

const PARENT_EMAIL = 'parent@example.com';
const SCHOOL_YEAR_START = new Date('2026-08-01');

function testClassroom() {
  const res = Classroom.Courses.list({ courseStates: ['ACTIVE'] });
  const courses = res.courses || [];
  Logger.log(courses.length + ' active courses');
  courses.forEach(c => Logger.log('- ' + c.name));
  if (courses.length) {
    const work = Classroom.Courses.CourseWork.list(courses[0].id, { pageSize: 3 });
    const titles = (work.courseWork || []).map(w => w.title);
    Logger.log('Sample coursework in "' + courses[0].name + '": ' + JSON.stringify(titles));
  }
}

function shareClassroomWithParent() {
  let shared = 0, already = 0, skipped = 0, failed = 0;
  const me = Session.getActiveUser().getEmail();
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
          // Classroom fills this folder with shortcuts; share the target, not the shortcut.
          const target = f.getMimeType() === 'application/vnd.google-apps.shortcut'
            ? DriveApp.getFileById(f.getTargetId())
            : f;
          // Only the student's own files. Group docs and teacher materials carry
          // other people's work and must never be shared outside the domain.
          if (target.getOwner().getEmail() !== me) { skipped++; continue; }
          const people = target.getViewers().concat(target.getEditors()).map(u => u.getEmail());
          if (people.indexOf(PARENT_EMAIL) >= 0) { already++; continue; }
          target.addViewer(PARENT_EMAIL);
          shared++;
        } catch (e) {
          failed++;
          Logger.log('Could not share one file in "' + folder.getName() + '": ' + e.message);
        }
      }
    }
  }
  Logger.log('Shared ' + shared + ' new, ' + already + ' already shared, '
    + skipped + ' not owned by student (skipped), ' + failed + ' could not be shared.');
}
