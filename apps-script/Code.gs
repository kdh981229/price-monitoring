/** GitHub Actions -> Google Drive validation and immutable storage gateway. */

const ROOT_FOLDER_ID = '1A6JFEZZqFfSkecTzPebZmgPZtRrN54rN';

function doGet() {
  return jsonResponse_({ok: true, service: 'price-monitoring-drive-gateway'});
}

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const expectedSecret = PropertiesService.getScriptProperties().getProperty('SHARED_SECRET');
    if (!expectedSecret || payload.secret !== expectedSecret) {
      throw new Error('unauthorized');
    }
    requireFields_(payload, ['run_id', 'json_name', 'json_base64', 'json_sha256']);
    if (!/^run-\d{8}T\d{6}[+-]\d{4}$/.test(payload.run_id)) {
      throw new Error('invalid run_id');
    }
    const jsonBytes = Utilities.base64Decode(payload.json_base64);
    verifySha256_(jsonBytes, payload.json_sha256);
    const jsonText = Utilities.newBlob(jsonBytes).getDataAsString('UTF-8');
    const parsed = JSON.parse(jsonText);
    if (parsed.record_type !== 'immutable_scan_run' || parsed.run.run_id !== payload.run_id) {
      throw new Error('JSON identity mismatch');
    }
    const dateMatch = payload.run_id.match(/^run-(\d{4})(\d{2})(\d{2})/);
    const root = DriveApp.getFolderById(ROOT_FOLDER_ID);
    const history = ensurePath_(root, ['history', dateMatch[1], dateMatch[2], dateMatch[3]]);
    const immutable = createImmutable_(history, payload.json_name, jsonBytes, payload.json_sha256, 'application/json');

    const current = ensurePath_(root, ['current']);
    const index = {
      schema_version: '1.0',
      record_type: 'latest_pointer',
      run_id: payload.run_id,
      immutable_file_id: immutable.getId(),
      immutable_file_url: immutable.getUrl(),
      sha256: payload.json_sha256,
      updated_at: new Date().toISOString()
    };
    replaceNamedFile_(current, 'latest.json', JSON.stringify(index, null, 2) + '\n', 'application/json');

    let briefingId = null;
    if (payload.briefing_base64) {
      requireFields_(payload, ['briefing_name', 'briefing_sha256']);
      const briefingBytes = Utilities.base64Decode(payload.briefing_base64);
      verifySha256_(briefingBytes, payload.briefing_sha256);
      const briefingFolder = ensurePath_(root, ['briefings', dateMatch[1], dateMatch[2]]);
      const briefing = createImmutable_(briefingFolder, payload.briefing_name, briefingBytes, payload.briefing_sha256, 'text/markdown');
      briefingId = briefing.getId();
    }
    return jsonResponse_({ok: true, run_id: payload.run_id, immutable_file_id: immutable.getId(), briefing_file_id: briefingId});
  } catch (error) {
    console.error(error);
    return jsonResponse_({ok: false, error: String(error && error.message ? error.message : error)});
  }
}

function ensurePath_(root, parts) {
  let folder = root;
  parts.forEach(function(name) {
    const matches = folder.getFoldersByName(name);
    folder = matches.hasNext() ? matches.next() : folder.createFolder(name);
  });
  return folder;
}

function createImmutable_(folder, name, bytes, sha256, mimeType) {
  const matches = folder.getFilesByName(name);
  if (matches.hasNext()) {
    const existing = matches.next();
    const existingSha = sha256Hex_(existing.getBlob().getBytes());
    if (existingSha !== sha256.toLowerCase()) {
      throw new Error('immutable conflict for ' + name);
    }
    return existing;
  }
  const blob = Utilities.newBlob(bytes, mimeType, name);
  return folder.createFile(blob);
}

function replaceNamedFile_(folder, name, text, mimeType) {
  const matches = folder.getFilesByName(name);
  while (matches.hasNext()) {
    matches.next().setTrashed(true);
  }
  return folder.createFile(Utilities.newBlob(text, mimeType, name));
}

function verifySha256_(bytes, expected) {
  const actual = sha256Hex_(bytes);
  if (actual !== String(expected).toLowerCase()) {
    throw new Error('sha256 mismatch');
  }
}

function sha256Hex_(bytes) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes)
    .map(function(value) { return ('0' + (value & 0xff).toString(16)).slice(-2); })
    .join('');
}

function requireFields_(object, fields) {
  fields.forEach(function(field) {
    if (!object[field]) throw new Error('missing field: ' + field);
  });
}

function jsonResponse_(value) {
  return ContentService.createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}

