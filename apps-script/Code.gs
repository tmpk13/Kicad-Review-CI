// hw-review Drive bridge: a web app the relay calls to list and download files in the
// team's shared drive, and to add the review report there.
//
// Deploy as a web app: Execute as "Me", Who has access "Anyone". The relay proves
// itself with a shared key. Script properties (Project Settings > Script properties):
//   RELAY_KEY       the shared key; the same value is APPS_SCRIPT_KEY on the relay
//   ALLOWED_ROOTS   comma-separated folder IDs. Every read and every upload must lie
//                   inside one of them, whatever the relay asks for.
//   MAX_FILE_BYTES  optional download cap per file, default 25000000
//
// Requests (query parameters; an upload's body is the file, base64-encoded):
//   action=list&roots=ID,ID       every file under the roots, recursively
//   action=get&roots=ID,ID&id=ID  one file as base64; Google Docs come back as Markdown,
//                                 Sheets as .xlsx, Slides and Drawings as PDF
//   action=upload&folder=ID&name=NAME.docx   add a new file; existing files are never changed
//   action=ping
// Drive is reached through its REST API (v3), which handles shared drives explicitly.

var DRIVE_API = 'https://www.googleapis.com/drive/v3/files';
var UPLOAD_API = 'https://www.googleapis.com/upload/drive/v3/files';
var FOLDER = 'application/vnd.google-apps.folder';
var SHORTCUT = 'application/vnd.google-apps.shortcut';
var EXPORTS = {
  'application/vnd.google-apps.document': { mime: 'text/markdown', ext: '.md' },
  'application/vnd.google-apps.spreadsheet': {
    mime: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', ext: '.xlsx' },
  'application/vnd.google-apps.presentation': { mime: 'application/pdf', ext: '.pdf' },
  'application/vnd.google-apps.drawing': { mime: 'application/pdf', ext: '.pdf' }
};
var UPLOAD_TYPES = {
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  md: 'text/markdown',
  pdf: 'application/pdf'
};
var ID_RE = /^[A-Za-z0-9_-]{10,100}$/;
var LIST_LIMIT = 5000;
var LIST_SECONDS = 300;

function doGet(e) {
  return respond_(e, '');
}

function doPost(e) {
  return respond_(e, e.postData ? e.postData.contents : '');
}

function respond_(e, body) {
  var out;
  try {
    out = handle_((e && e.parameter) || {}, body);
  } catch (err) {
    out = { ok: false, error: String((err && err.message) || err) };
  }
  return ContentService.createTextOutput(JSON.stringify(out)).setMimeType(ContentService.MimeType.JSON);
}

function handle_(p, body) {
  var props = PropertiesService.getScriptProperties();
  var key = props.getProperty('RELAY_KEY');
  if (!key || !sameSecret_(String(p.key || ''), key)) return { ok: false, error: 'unauthorized' };
  var allowed = ids_(props.getProperty('ALLOWED_ROOTS'));
  var maxBytes = Number(props.getProperty('MAX_FILE_BYTES') || 25000000);
  switch (p.action) {
    case 'ping':
      return { ok: true };
    case 'list':
      return list_(roots_(p.roots, allowed));
    case 'get':
      return get_(String(p.id || ''), roots_(p.roots, allowed), maxBytes);
    case 'upload':
      return upload_(String(p.folder || ''), String(p.name || ''), body, allowed);
    default:
      return { ok: false, error: 'unknown action' };
  }
}

// Compare SHA-256 digests in full, so the time taken does not depend on where they differ.
function sameSecret_(a, b) {
  var x = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, a);
  var y = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, b);
  var diff = x.length ^ y.length;
  for (var i = 0; i < x.length && i < y.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function ids_(value) {
  return String(value || '').split(',').map(function (s) { return s.trim(); })
    .filter(function (s) { return ID_RE.test(s); });
}

// The requested roots that ALLOWED_ROOTS permits; an empty result is an error.
function roots_(requested, allowed) {
  var roots = ids_(requested).filter(function (r) { return allowed.indexOf(r) >= 0; });
  if (!roots.length) throw new Error('none of the requested folders is in ALLOWED_ROOTS');
  return roots;
}

function query_(params) {
  return Object.keys(params).filter(function (k) { return params[k] !== '' && params[k] !== undefined; })
    .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(String(params[k])); }).join('&');
}

function api_(url, options) {
  var opts = options || {};
  opts.headers = { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() };
  opts.muteHttpExceptions = true;
  var resp = UrlFetchApp.fetch(url, opts);
  var code = resp.getResponseCode();
  if (code >= 300) throw new Error('Drive API HTTP ' + code + ': ' + resp.getContentText().slice(0, 300));
  return resp;
}

function meta_(id, fields) {
  return JSON.parse(api_(DRIVE_API + '/' + id + '?' + query_({ fields: fields, supportsAllDrives: true })).getContentText());
}

// A Drive name as one path component.
function clean_(name) {
  var s = String(name || '').replace(/[\/\\\u0000-\u001f]/g, '_').trim().slice(0, 150);
  return s && s !== '.' && s !== '..' ? s : 'unnamed';
}

function list_(roots) {
  var started = Date.now();
  var files = [];
  var seen = {};
  var queue = roots.map(function (id) { return { id: id, path: clean_(meta_(id, 'id,name').name) }; });
  while (queue.length) {
    var folder = queue.shift();
    if (seen[folder.id]) continue;
    seen[folder.id] = true;
    var pageToken = '';
    do {
      var page = JSON.parse(api_(DRIVE_API + '?' + query_({
        q: "'" + folder.id + "' in parents and trashed = false",
        fields: 'nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum)',
        pageSize: 1000,
        supportsAllDrives: true,
        includeItemsFromAllDrives: true,
        pageToken: pageToken
      })).getContentText());
      (page.files || []).forEach(function (f) {
        var path = folder.path + '/' + clean_(f.name);
        if (f.mimeType === FOLDER) {
          queue.push({ id: f.id, path: path });
        } else if (f.mimeType !== SHORTCUT) {
          // Shortcuts are left out: their targets can sit outside the allowed folders.
          files.push({ id: f.id, path: path, mimeType: f.mimeType, size: f.size ? Number(f.size) : null,
                       modified: f.modifiedTime || null, md5: f.md5Checksum || null });
        }
      });
      pageToken = page.nextPageToken || '';
    } while (pageToken);
    if (files.length >= LIST_LIMIT || Date.now() - started > LIST_SECONDS * 1000) {
      return { ok: true, files: files, truncated: true };
    }
  }
  return { ok: true, files: files };
}

// True when the item (by its metadata) lies inside one of the folders.
function under_(item, folders) {
  var cache = CacheService.getScriptCache();
  var frontier = item.parents || [];
  var seen = {};
  for (var depth = 0; depth < 30 && frontier.length; depth++) {
    var next = [];
    for (var i = 0; i < frontier.length; i++) {
      var id = frontier[i];
      if (folders.indexOf(id) >= 0) return true;
      if (seen[id]) continue;
      seen[id] = true;
      var cached = cache.get('parents:' + id);
      var parents = cached !== null ? JSON.parse(cached) : (meta_(id, 'id,parents').parents || []);
      if (cached === null) cache.put('parents:' + id, JSON.stringify(parents), 21600);
      next = next.concat(parents);
    }
    frontier = next;
  }
  return false;
}

function get_(id, roots, maxBytes) {
  if (!ID_RE.test(id)) return { ok: false, error: 'bad file id' };
  var meta = meta_(id, 'id,name,mimeType,size,parents');
  if (!under_(meta, roots)) return { ok: false, error: 'the file is outside the allowed folders' };
  var name = meta.name;
  var exportedAs = null;
  var blob;
  var exp = EXPORTS[meta.mimeType];
  if (exp) {
    blob = api_(DRIVE_API + '/' + id + '/export?' + query_({ mimeType: exp.mime })).getBlob();
    name = meta.name + exp.ext;
    exportedAs = exp.mime;
  } else if (meta.mimeType.indexOf('application/vnd.google-apps.') === 0) {
    return { ok: false, error: 'no download for Google type ' + meta.mimeType };
  } else {
    if (Number(meta.size || 0) > maxBytes) return { ok: false, error: 'the file is over ' + maxBytes + ' bytes' };
    blob = api_(DRIVE_API + '/' + id + '?' + query_({ alt: 'media', supportsAllDrives: true })).getBlob();
  }
  var bytes = blob.getBytes();
  if (bytes.length > maxBytes) return { ok: false, error: 'the file is over ' + maxBytes + ' bytes' };
  return { ok: true, id: id, name: clean_(name), mimeType: meta.mimeType, exportedAs: exportedAs,
           size: bytes.length, data: Utilities.base64Encode(bytes) };
}

function upload_(folder, name, body, allowed) {
  if (!ID_RE.test(folder)) return { ok: false, error: 'bad folder id' };
  var m = /^[A-Za-z0-9._ ()-]{1,150}\.(docx|md|pdf)$/.exec(name);
  if (!m) return { ok: false, error: 'bad file name' };
  var meta = meta_(folder, 'id,mimeType,parents');
  if (meta.mimeType !== FOLDER) return { ok: false, error: 'the upload target is not a folder' };
  if (allowed.indexOf(folder) < 0 && !under_(meta, allowed)) {
    return { ok: false, error: 'the upload folder is outside the allowed folders' };
  }
  var bytes = Utilities.base64Decode(String(body || '').trim());
  if (!bytes.length) return { ok: false, error: 'empty upload' };
  var mime = UPLOAD_TYPES[m[1]];
  var boundary = 'hwreview' + Utilities.getUuid().replace(/-/g, '');
  var head = '--' + boundary + '\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n' +
    JSON.stringify({ name: name, parents: [folder], mimeType: mime }) +
    '\r\n--' + boundary + '\r\nContent-Type: ' + mime + '\r\n\r\n';
  var payload = Utilities.newBlob(head).getBytes().concat(bytes)
    .concat(Utilities.newBlob('\r\n--' + boundary + '--').getBytes());
  var created = JSON.parse(api_(UPLOAD_API + '?' + query_({
    uploadType: 'multipart', supportsAllDrives: true, fields: 'id,name,webViewLink'
  }), { method: 'post', contentType: 'multipart/related; boundary=' + boundary, payload: payload }).getContentText());
  return { ok: true, id: created.id, name: created.name, url: created.webViewLink };
}

// Run from the editor once: it asks for the Drive permission and logs what the relay
// would see. Check the log (View > Logs) before deploying.
function selfTest() {
  var props = PropertiesService.getScriptProperties();
  var allowed = ids_(props.getProperty('ALLOWED_ROOTS'));
  if (!props.getProperty('RELAY_KEY')) Logger.log('RELAY_KEY is not set');
  if (!allowed.length) Logger.log('ALLOWED_ROOTS is not set');
  allowed.forEach(function (root) {
    var listing = list_([root]);
    Logger.log(root + ': ' + listing.files.length + ' files' + (listing.truncated ? ' (truncated)' : ''));
    listing.files.slice(0, 10).forEach(function (f) { Logger.log('  ' + f.path + ' (' + f.mimeType + ')'); });
  });
}
