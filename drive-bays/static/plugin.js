// drive-bays — physical drive-bay map.
//
// Renders the chassis as a grid of bays in their real positions, with the
// drive in each one. Globals are all prefixed `dbay` (or namespaced under the
// page/dashcard conventions) so nothing here can clobber a core name.
//
// Drawn with CSS grid rather than a canvas: it inherits the dashboard theme
// automatically, works in light and dark, stays legible at any zoom, and keeps
// the plugin dependency-free.

let _dbayData = null;
let _dbayHealth = null;

function dbayStatusClass(bay) {
  if (!bay.occupied) return 'empty';
  if (bay.fault) return 'red';
  if (bay.status && bay.status !== 'OK') return 'yellow';
  const h = _dbayHealth && _dbayHealth[bay.dev];
  if (h && h.available && h.health === 'FAILED') return 'red';
  if (h && h.available && h.health === 'unknown') return 'yellow';
  return 'green';
}

function dbayTile(encId, bay) {
  const cls = dbayStatusClass(bay);
  const h = (_dbayHealth && _dbayHealth[bay.dev]) || null;
  const temp = h && h.available && h.temperature_c != null
    ? `<div class="dbay-temp">${escapeHtml(String(h.temperature_c))}&deg;C</div>` : '';
  const body = bay.occupied
    ? `<div class="dbay-dev">${escapeHtml(bay.dev)}</div>
       <div class="dbay-size">${escapeHtml(bay.size || '')} ${escapeHtml(bay.disk_type || '')}</div>
       ${temp}`
    : `<div class="dbay-empty-text">empty</div>`;
  return `
    <div class="dbay ${cls}${bay.locate ? ' locating' : ''}"
         onclick="dbayDetail('${jsArg(encId)}','${jsArg(String(bay.slot))}')"
         title="${escapeHtml('Bay ' + bay.label + (bay.occupied ? ' — /dev/' + bay.dev : ' — empty'))}">
      <div class="dbay-label">${escapeHtml(bay.label)}</div>
      ${body}
    </div>`;
}

function dbayGrid(enc) {
  const rows = enc.rows || 1;
  const perRow = Math.max(1, Math.ceil(enc.bays.length / rows));
  return `
    <div class="dbay-grid" style="grid-template-columns: repeat(${perRow}, minmax(74px, 1fr))">
      ${enc.bays.map(b => dbayTile(enc.id, b)).join('')}
    </div>
    <div class="dbay-orient">
      <span>&larr; motherboard</span><span>front plate &rarr;</span>
    </div>`;
}

function dbayWarnings(d) {
  const out = [];
  (d.enclosures || []).forEach(e => {
    (e.label_mismatches || []).forEach(m => {
      out.push(`Bay label disagreement in enclosure ${escapeHtml(e.id)}: slot `
        + `${escapeHtml(String(m.slot))} is <code>${escapeHtml(m.vdev)}</code> per `
        + `/etc/vdev_id.conf but <code>${escapeHtml(m.derived)}</code> by position. `
        + `The vdev_id.conf label is shown.`);
    });
    // Padding slots (e.trimmed_slots) are deliberately NOT surfaced. Every
    // backplane advertises more array slots than the chassis has bays, so it
    // would be a permanent notice about entirely normal hardware — noise an
    // operator learns to ignore. Still in the API response for debugging.
  });
  if (d.enclosures && d.enclosures.length && !d.led_helper) {
    out.push(`Identify/fault LED control is unavailable — the helper is not `
      + `installed at <code>/usr/local/sbin/nexus-bay-led</code>. See the `
      + `plugin README.`);
  }
  if (!out.length) return '';
  return `<div class="alert alert-warning" style="margin-bottom:14px">
    ${out.map(w => `<div>${w}</div>`).join('')}</div>`;
}

function dbayChassisCard(d) {
  const c = d.chassis;
  const encs = d.enclosures || [];
  const bays = encs.reduce((n, e) => n + e.bays.length, 0);
  const occ = encs.reduce((n, e) => n + e.occupied, 0);
  const cells = [
    ['Chassis', c && c.model ? c.model : 'Unknown'],
    ['Serial', c && c.serial ? c.serial : '—'],
    ['Enclosure', encs.map(e => `${e.vendor} ${e.model}`.trim() || e.id).join(', ') || '—'],
    ['Bays', `${occ} of ${bays} occupied`],
  ];
  return `<div class="card-grid">
    ${cells.map(([k, v]) => `<div class="card">
      <div class="card-value" style="font-size:15px">${escapeHtml(String(v))}</div>
      <div class="card-label">${escapeHtml(k)}</div></div>`).join('')}
  </div>`;
}

// A passive M.2 carrier has no switch, no controller and no PCI ID — nothing
// on it enumerates, so the card is NEVER named. What is real is the group
// (drives sharing a PCI root-port device) and the order within it (the root
// port's function number). Both are labelled as derived, not as gospel.
function dbayCarriers(d) {
  const cards = d.carriers || [];
  if (!cards.length) return '';
  return `
    <h2 style="margin-top:28px">M.2 carrier cards</h2>
    <p class="help">Drives grouped by the PCIe slot they share. A passive
      bifurcation carrier has nothing on it that enumerates, so the card model
      cannot be identified — only the slot it sits in and the order of the
      sockets. Socket numbering runs in lane order; which end is socket 1 is a
      card-layout convention, so confirm it once against your hardware.</p>
    ${cards.map(c => `
      <h3 class="dbay-enc-title">${escapeHtml(c.label)}
        <span class="help">${escapeHtml(c.slots + '-socket carrier'
          + (c.slot_known ? '' : ' · slot name needs dmidecode'))}</span></h3>
      <div class="dbay-grid" style="grid-template-columns: repeat(${c.slots}, minmax(150px, 1fr))">
        ${c.members.map(m => `
          <div class="dbay green" style="cursor:default" title="${escapeHtml(
              '/dev/' + m.dev + ' — ' + (m.model || '') + ' at ' + m.pci)}">
            <div class="dbay-label">SOCKET ${escapeHtml(String(m.position))}</div>
            <div class="dbay-dev">${escapeHtml(m.dev)}</div>
            <div class="dbay-size">${escapeHtml(m.size || '')} ·
              ${escapeHtml(m.model || '')}</div>
            <div class="dbay-temp">${escapeHtml(
              (m.link_width ? 'x' + m.link_width + ' ' : '')
              + (m.link_speed || ''))}</div>
          </div>`).join('')}
      </div>
      <div class="dbay-orient"><span>${escapeHtml(c.group)}</span>
        <span>${escapeHtml(c.members.map(m => m.usage).filter((v, i, a) =>
          a.indexOf(v) === i).join(', '))}</span></div>`).join('')}`;
}

function dbayUnassigned(d) {
  const rows = d.unassigned || [];
  if (!rows.length) return '';
  return `
    <h2 style="margin-top:28px">Not in a bay</h2>
    <p class="help">Disks with no physical position the system can report —
      onboard M.2, SATA, USB. They are real devices with no chassis bay and no
      carrier group, so they are listed rather than placed on a map.</p>
    <table class="table">
      <thead><tr><th>Device</th><th>Size</th><th>Type</th><th>Transport</th>
        <th>Model</th><th>Usage</th></tr></thead>
      <tbody>
        ${rows.map(r => `<tr>
          <td class="mono">${escapeHtml(r.dev || '')}</td>
          <td>${escapeHtml(r.size || '')}</td>
          <td>${escapeHtml(r.disk_type || '')}</td>
          <td>${escapeHtml(r.transport || '')}</td>
          <td>${escapeHtml(r.model || '')}</td>
          <td>${escapeHtml(r.usage || '')}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

window['page_drive-bays'] = async function () {
  let d;
  try {
    d = await API.get('/api/drive-bays');
  } catch (e) {
    $('page-content').innerHTML = `<h2>Drive Bays</h2>
      <div class="alert alert-danger">${escapeHtml(e.message || String(e))}</div>`;
    return;
  }
  _dbayData = d;

  if (!d.available) {
    $('page-content').innerHTML = `
      <h2>Drive Bays</h2>
      <div class="alert alert-warning">${escapeHtml(d.reason || 'No enclosure found.')}</div>
      ${dbayCarriers(d)}
      ${dbayUnassigned(d)}`;
    return;
  }

  $('page-content').innerHTML = `
    <div class="page-header">
      <h2>Drive Bays</h2>
      <div>
        <button class="btn btn-sm btn-outline" onclick="dbayLoadHealth()"
                id="dbay-health-btn">Read SMART</button>
        <button class="btn btn-sm btn-outline" onclick="renderPage('drive-bays')">Refresh</button>
      </div>
    </div>
    ${dbayChassisCard(d)}
    ${dbayWarnings(d)}
    ${d.enclosures.map(e => `
      <h3 class="dbay-enc-title">${escapeHtml(e.vendor + ' ' + e.model)}
        <span class="help">${escapeHtml(e.id)}</span></h3>
      ${dbayGrid(e)}`).join('')}
    ${dbayCarriers(d)}
    ${dbayUnassigned(d)}
    <style>
      .dbay-grid { display: grid; gap: 6px; margin: 14px 0 4px; }
      .dbay {
        border: 1px solid var(--border); border-radius: var(--radius);
        background: var(--card-bg); padding: 7px 6px; cursor: pointer;
        min-height: 74px; display: flex; flex-direction: column; gap: 2px;
        border-left-width: 3px;
      }
      .dbay:hover { border-color: var(--text-muted); }
      .dbay.green { border-left-color: var(--green); }
      .dbay.yellow { border-left-color: var(--yellow); }
      .dbay.red { border-left-color: var(--red); }
      .dbay.empty {
        border-style: dashed; border-left-width: 1px;
        background: transparent; color: var(--text-muted);
      }
      .dbay.locating { outline: 2px solid var(--primary); outline-offset: 1px; }
      .dbay-label { font-size: 11px; color: var(--text-muted); letter-spacing: .04em; }
      .dbay-dev { font-family: var(--mono); font-size: 13px; }
      .dbay-size { font-size: 11px; color: var(--text-muted); }
      .dbay-temp { font-family: var(--mono); font-size: 11px; margin-top: auto; }
      .dbay-empty-text { font-size: 11px; margin: auto 0; }
      .dbay-orient {
        display: flex; justify-content: space-between;
        font-size: 11px; color: var(--text-muted); margin-bottom: 18px;
      }
      .dbay-enc-title { font-size: 13px; margin: 20px 0 0; font-weight: 600; }
      .dbay-enc-title .help { font-family: var(--mono); margin-left: 8px; }
    </style>`;
};

// SMART is a separate, explicitly-triggered call: it runs smartctl per drive
// and a full chassis can hold 60. The map is already on screen by then.
async function dbayLoadHealth() {
  const btn = $('dbay-health-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Reading…'; }
  try {
    const j = await API.get('/api/drive-bays/health');
    _dbayHealth = j.health || {};
    // Re-render tiles in place so temperature and health colour appear.
    (_dbayData.enclosures || []).forEach(e => {
      const grids = document.querySelectorAll('.dbay-grid');
      grids.forEach((g, i) => {
        if (_dbayData.enclosures[i] && _dbayData.enclosures[i].id === e.id) {
          g.innerHTML = e.bays.map(b => dbayTile(e.id, b)).join('');
        }
      });
    });
  } catch (e) {
    alert(e.message || String(e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Read SMART'; }
  }
}

function dbayFindBay(encId, slot) {
  const enc = (_dbayData.enclosures || []).find(e => e.id === encId);
  if (!enc) return [null, null];
  return [enc, enc.bays.find(b => String(b.slot) === String(slot)) || null];
}

function dbayRow(k, v) {
  if (v == null || v === '') return '';
  return `<tr><td style="color:var(--text-muted);width:40%">${escapeHtml(k)}</td>
    <td class="mono">${escapeHtml(String(v))}</td></tr>`;
}

async function dbayDetail(encId, slot) {
  const [enc, bay] = dbayFindBay(encId, slot);
  if (!bay) return;

  const ledBtns = (currentRole === 'admin' && bay.led_writable && _dbayData.led_helper)
    ? `<div style="margin-top:12px">
         <button class="btn btn-sm" onclick="dbayLed('${jsArg(encId)}','${jsArg(String(bay.component))}','locate',${bay.locate ? 'false' : 'true'})">
           ${bay.locate ? 'Stop identifying' : 'Identify (blink LED)'}</button>
         <button class="btn btn-sm btn-outline" onclick="dbayLed('${jsArg(encId)}','${jsArg(String(bay.component))}','fault',${bay.fault ? 'false' : 'true'})">
           ${bay.fault ? 'Clear fault LED' : 'Set fault LED'}</button>
       </div>`
    : (bay.led_writable && !_dbayData.led_helper
        ? `<p class="help" style="margin-top:12px">LED control needs the
             <code>nexus-bay-led</code> helper — see the plugin README.</p>`
        : '');

  const identity = `
    <table class="table">
      ${dbayRow('Bay', bay.label + '  (label from ' + bay.label_source + ')')}
      ${dbayRow('Enclosure slot', bay.slot)}
      ${dbayRow('Enclosure', enc.vendor + ' ' + enc.model + '  ' + enc.id)}
      ${dbayRow('Enclosure status', bay.status)}
      ${dbayRow('Device', bay.occupied ? '/dev/' + bay.dev : 'empty')}
      ${dbayRow('Model', bay.model)}
      ${dbayRow('Serial', bay.serial)}
      ${dbayRow('Capacity', bay.size)}
      ${dbayRow('Type', bay.disk_type)}
      ${dbayRow('Transport', bay.transport)}
      ${dbayRow('Partitions', bay.occupied ? bay.partitions : '')}
      ${dbayRow('Usage', bay.usage)}
    </table>`;

  openModal('Bay ' + bay.label, identity
    + `<div id="dbay-smart"></div>` + ledBtns, {wide: true});

  if (!bay.occupied) return;
  const cached = _dbayHealth && _dbayHealth[bay.dev];
  if (cached) { dbayRenderSmart(cached); return; }
  const el = $('dbay-smart');
  if (el) el.innerHTML = `<p class="help">Reading SMART…</p>`;
  try {
    const j = await API.get('/api/drive-bays/health');
    _dbayHealth = j.health || {};
    dbayRenderSmart(_dbayHealth[bay.dev]);
  } catch (e) {
    if ($('dbay-smart')) $('dbay-smart').innerHTML =
      `<p class="help">SMART unavailable: ${escapeHtml(e.message || String(e))}</p>`;
  }
}

function dbayRenderSmart(h) {
  const el = $('dbay-smart');
  if (!el) return;
  if (!h || !h.available) {
    el.innerHTML = `<p class="help">SMART unavailable${h && h.error ? ': ' + escapeHtml(h.error) : ''}</p>`;
    return;
  }
  const cls = h.health === 'OK' ? 'green' : h.health === 'FAILED' ? 'red' : 'yellow';
  el.innerHTML = `
    <h3 style="margin:16px 0 6px;font-size:13px">SMART</h3>
    <p><span class="status-badge ${cls}">${escapeHtml(h.health || 'unknown')}</span></p>
    <table class="table">
      ${dbayRow('Temperature', h.temperature_c != null ? h.temperature_c + ' °C' : '')}
      ${dbayRow('Power-on hours', h.power_on_hours)}
      ${dbayRow('Power cycles', h.power_cycles)}
      ${dbayRow('Start/stop count', h.start_stop)}
      ${dbayRow('Firmware', h.firmware)}
      ${dbayRow('Rotation rate', h.rotation_rate)}
      ${dbayRow('Reallocated sectors', h.reallocated)}
      ${dbayRow('Pending sectors', h.pending)}
      ${dbayRow('Offline uncorrectable', h.uncorrectable)}
      ${dbayRow('Media errors', h.media_errors)}
      ${dbayRow('Wear used', h.percentage_used != null ? h.percentage_used + '%' : '')}
    </table>`;
}

async function dbayLed(encId, component, attr, on) {
  try {
    await API.post('/api/drive-bays/led',
      {enclosure: encId, component: component, attr: attr, on: on});
    closeModal();
    await renderPage('drive-bays');
  } catch (e) {
    alert(e.message || String(e));
  }
}

// Dashboard front-page card: occupancy at a glance, red when the backplane
// itself flags a bay. The global MUST be named dashcard_<module id>, which is
// why it is assigned rather than declared (the id contains a hyphen).
window['dashcard_drive-bays'] = function (ctx) {
  const s = ((ctx && ctx.s) || {})['drive-bays'];
  if (!s || !s.available) return '';
  const color = s.faulted ? 'var(--red)' : 'var(--green)';
  return `<div class="card" onclick="showPage('drive-bays')" style="cursor:pointer">
    <div class="card-value" style="color:${color}">${s.occupied}/${s.bays}</div>
    <div class="card-label">Drive bays occupied${s.faulted ? ' · ' + s.faulted + ' fault' : ''}</div>
  </div>`;
};
