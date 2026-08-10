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
    if (payload.action === 'slot_status') {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(String(payload.date || ''))) throw new Error('invalid date');
      if (payload.scheduled_slot_hour_kst === undefined) throw new Error('missing field: scheduled_slot_hour_kst');
      const slot = Number(payload.scheduled_slot_hour_kst);
      if ([0, 4, 8, 12, 16, 20].indexOf(slot) === -1) throw new Error('invalid scheduled_slot_hour_kst');
      const root = DriveApp.getFolderById(ROOT_FOLDER_ID);
      return jsonResponse_({ok: true, completed: slotCompleted_(root, String(payload.date), slot)});
    }
    if (payload.action === 'watchdog_check') {
      return jsonResponse_(storeWatchdogCheck_(payload));
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
      // The 09:15 KST fallback may legitimately repeat an already completed 08시 run.
      // Keep the first briefing immutable, but still accept and archive the fallback raw JSON.
      const briefing = createBriefingOnce_(briefingFolder, payload.briefing_name, briefingBytes, payload.briefing_sha256);
      briefingId = briefing.getId();
    }
    return jsonResponse_({ok: true, run_id: payload.run_id, immutable_file_id: immutable.getId(), briefing_file_id: briefingId});
  } catch (error) {
    console.error(error);
    return jsonResponse_({ok: false, error: String(error && error.message ? error.message : error)});
  }
}

function storeWatchdogCheck_(payload) {
  requireFields_(payload, ['report_name', 'report_base64', 'report_sha256']);
  if (!/^watchdog-\d{8}-(00|04|08|12|16|20)\.json$/.test(payload.report_name)) {
    throw new Error('invalid watchdog report_name');
  }
  const reportBytes = Utilities.base64Decode(payload.report_base64);
  verifySha256_(reportBytes, payload.report_sha256);
  const reportText = Utilities.newBlob(reportBytes).getDataAsString('UTF-8');
  const report = JSON.parse(reportText);
  if (report.record_type !== 'watchdog_check' || payload.report_name !== report.check_id + '.json') {
    throw new Error('watchdog identity mismatch');
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(report.expected_date_kst || ''))) {
    throw new Error('invalid watchdog date');
  }
  const slot = Number(report.expected_slot_hour_kst);
  if ([0, 4, 8, 12, 16, 20].indexOf(slot) === -1) throw new Error('invalid watchdog slot');
  if (['healthy', 'missing'].indexOf(report.outcome) === -1) throw new Error('invalid watchdog outcome');
  const compactDate = String(report.expected_date_kst).replace(/-/g, '');
  if (report.check_id !== 'watchdog-' + compactDate + '-' + ('0' + slot).slice(-2)) {
    throw new Error('watchdog check_id mismatch');
  }

  const dateParts = String(report.expected_date_kst).split('-');
  const root = DriveApp.getFolderById(ROOT_FOLDER_ID);
  const logFolder = ensurePath_(root, ['logs', 'watchdog', dateParts[0], dateParts[1], dateParts[2]]);
  const storedLog = createFirstWins_(logFolder, payload.report_name, reportBytes, 'application/json');

  let incidentId = null;
  let incidentCreated = false;
  if (report.outcome === 'missing') {
    const incident = {
      schema_version: '1.0',
      record_type: 'collection_incident',
      incident_id: 'missing-slot-' + compactDate + '-' + ('0' + slot).slice(-2),
      status: 'open',
      first_detected_at: report.checked_at,
      expected_date_kst: report.expected_date_kst,
      expected_slot_hour_kst: slot,
      watchdog_check_id: report.check_id,
      watchdog_log_file_id: storedLog.file.getId(),
      workflow_url: report.workflow_url || null,
      message: report.message
    };
    const incidentFolder = ensurePath_(root, ['incidents', dateParts[0], dateParts[1], dateParts[2]]);
    const incidentName = incident.incident_id + '.json';
    const storedIncident = createTextFirstWins_(incidentFolder, incidentName, JSON.stringify(incident, null, 2) + '\n', 'application/json');
    incidentId = storedIncident.file.getId();
    incidentCreated = storedIncident.created;
  }
  return {
    ok: true,
    check_id: report.check_id,
    completed: report.outcome === 'healthy',
    log_file_id: storedLog.file.getId(),
    log_created: storedLog.created,
    incident_file_id: incidentId,
    incident_created: incidentCreated
  };
}

function ensurePath_(root, parts) {
  let folder = root;
  parts.forEach(function(name) {
    const matches = folder.getFoldersByName(name);
    folder = matches.hasNext() ? matches.next() : folder.createFolder(name);
  });
  return folder;
}

function slotCompleted_(root, date, slot) {
  const parts = date.split('-');
  let folder = root;
  for (const name of ['history', parts[0], parts[1], parts[2]]) {
    const matches = folder.getFoldersByName(name);
    if (!matches.hasNext()) return false;
    folder = matches.next();
  }
  const files = folder.getFiles();
  while (files.hasNext()) {
    try {
      const parsed = JSON.parse(files.next().getBlob().getDataAsString('UTF-8'));
      if (parsed.record_type === 'immutable_scan_run' && parsed.run && Number(parsed.run.scheduled_slot_hour_kst) === slot) return true;
    } catch (error) {
      // A non-JSON file or corrupt historical item must not block a fallback collection.
    }
  }
  return false;
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

function createBriefingOnce_(folder, name, bytes, sha256) {
  const matches = folder.getFilesByName(name);
  if (matches.hasNext()) return matches.next();
  return folder.createFile(Utilities.newBlob(bytes, 'text/markdown', name));
}

function createFirstWins_(folder, name, bytes, mimeType) {
  const matches = folder.getFilesByName(name);
  if (matches.hasNext()) return {file: matches.next(), created: false};
  return {file: folder.createFile(Utilities.newBlob(bytes, mimeType, name)), created: true};
}

function createTextFirstWins_(folder, name, text, mimeType) {
  const matches = folder.getFilesByName(name);
  if (matches.hasNext()) return {file: matches.next(), created: false};
  return {file: folder.createFile(Utilities.newBlob(text, mimeType, name)), created: true};
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

