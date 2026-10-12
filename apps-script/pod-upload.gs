/**
 * RTV — proof-of-delivery photo upload into Google Drive.
 *
 * The RTV app POSTs each POD photo here; this saves it into the POD folder
 * (one sub-folder per month) and returns the file's Drive link, which the
 * app keeps on the invoice. Runs as the folder's owner, so photos use that
 * account's Drive storage.
 *
 * Setup (once):
 *   Project Settings > Script properties:
 *     ROOT_FOLDER_ID = the POD folder's id (the part after /folders/ in its URL)
 *     UPLOAD_SECRET  = the same value as POD_UPLOAD_SECRET in the app's settings
 *   Run `authorize` once and allow Drive access.
 *   Deploy > New deployment > Web app — Execute as: Me, Who has access: Anyone.
 *   ("Anyone" lets the app's server reach it; the secret is what keeps others out.)
 */

function doPost(e) {
  try {
    var props = PropertiesService.getScriptProperties();
    var body = JSON.parse(e.postData.contents);
    if (!body.secret || body.secret !== props.getProperty('UPLOAD_SECRET')) {
      return json_({ ok: false, error: 'Not allowed' });
    }
    var root = DriveApp.getFolderById(props.getProperty('ROOT_FOLDER_ID'));
    var month = String(body.month || '').replace(/[^0-9-]/g, '') ||
      Utilities.formatDate(new Date(), 'Asia/Kolkata', 'yyyy-MM');
    var folder = monthFolder_(root, month);
    var name = String(body.filename || 'pod.jpg').replace(/[\\\/:*?"<>|]/g, '_');
    var blob = Utilities.newBlob(Utilities.base64Decode(body.dataBase64), body.mimeType || 'image/jpeg', name);
    var file = folder.createFile(blob);
    return json_({ ok: true, id: file.getId(), url: file.getUrl() });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

// Two uploads in the same new month must not create two "2026-10" folders.
function monthFolder_(root, name) {
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    var found = root.getFoldersByName(name);
    return found.hasNext() ? found.next() : root.createFolder(name);
  } finally {
    lock.releaseLock();
  }
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Run once from the editor to grant Drive access before deploying.
function authorize() {
  DriveApp.getRootFolder();
}
