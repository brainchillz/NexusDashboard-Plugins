"""drive-bays — physical drive-bay map for SES-capable enclosures.

Replicates the 45Drives "Disks" Houston plugin (bay layout, per-bay drive
identity + SMART, chassis identity) without depending on anything 45Drives
ships. The bay->device mapping is read from the kernel's SCSI Enclosure
Services driver at /sys/class/enclosure/, which is generic: it works on any
backplane or HBA that exposes SES, on any distro, and it additionally gives
real per-slot locate/fault LED control that a by-path mapping cannot.

45Drives' own files are used as OPTIONAL enrichment when present:
  /etc/45drives/server_info/server_info.json  chassis model/serial + HBA list
  /etc/vdev_id.conf                           authoritative bay LABELS
Neither is required; without them the plugin falls back to chassis-profile
labels and then to raw SES slot numbers.

Portability: standard library only, no f-string '=' specifiers, no walrus in
comprehensions, no match statements, no PEP 604 unions — valid on Python 3.9
(Rocky 9) through 3.14 (Ubuntu 26.04). Every external command goes through the
dashboard's argv-list run(); sysfs is read directly (world-readable) and only
LED WRITES need root, via the plugin's own helper.
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request

from nexusdash.core.runcmd import run, err
from nexusdash.core.auth import _is_admin

bp = Blueprint('drive-bays', __name__)

ENCLOSURE_ROOT = '/sys/class/enclosure'
SERVER_INFO = '/etc/45drives/server_info/server_info.json'
VDEV_ID_CONF = '/etc/vdev_id.conf'
LED_HELPER = '/usr/local/sbin/nexus-bay-led'

# SES component enumerations we treat as "not a real bay". A backplane
# commonly advertises more array-device slots than the chassis physically
# has; 'unsupported' is how the kernel reports those padding entries.
_PHANTOM_STATUS = ('unsupported',)

# Physical geometry is the ONE thing SES does not report: it hands out an
# ordinal slot number, never a position. Chassis profiles supply bay count and
# grid shape; 'bays' also trims the padding slots described above. Keyed by the
# 'Chassis Size' field of 45Drives' server_info.json, but usable standalone —
# add an entry for any chassis and the layout follows.
_CHASSIS_PROFILES = {
    'HL15':   {'bays': 15, 'rows': 1, 'label': '1-{n}'},
    'HL8':    {'bays': 8,  'rows': 1, 'label': '1-{n}'},
    'HL4':    {'bays': 4,  'rows': 1, 'label': '1-{n}'},
    'Q30':    {'bays': 30, 'rows': 2, 'label': '{row}-{col}'},
    'S45':    {'bays': 45, 'rows': 3, 'label': '{row}-{col}'},
    'XL60':   {'bays': 60, 'rows': 4, 'label': '{row}-{col}'},
}

RE_ENCLOSURE_ID = re.compile(r'\A[0-9]{1,4}:[0-9]{1,4}:[0-9]{1,4}:[0-9]{1,4}\Z')
RE_COMPONENT = re.compile(r'\A[0-9]{1,4}\Z')
RE_DEV = re.compile(r'\A[a-z0-9]{1,32}\Z')
RE_VDEV_ALIAS = re.compile(r'^alias\s+(\S+)\s+(\S+)')

# PCI bus:device.function as it appears in a /sys/devices/... path component.
RE_BDF = re.compile(r'\A[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]\Z')

PCI_DEVICES = '/sys/bus/pci/devices'


def _read(path, default=''):
    """Read a sysfs attribute. Absent/unreadable attributes are normal (not
    every SES implementation exposes every one), so failure is not an error."""
    try:
        with open(path, 'r') as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return default


def _read_flag(path):
    return _read(path) == '1'


def _component_dirs(enc_path):
    """Component subdirectories of an enclosure, numerically sorted.

    The kernel names them by ordinal ('0'..'47'); everything else in the
    directory (device, power, subsystem, uevent, ...) is filtered out by
    requiring a numeric name and a 'slot' attribute.
    """
    out = []
    try:
        names = os.listdir(enc_path)
    except OSError:
        return out
    for name in names:
        if not RE_COMPONENT.match(name):
            continue
        cpath = os.path.join(enc_path, name)
        if os.path.isfile(os.path.join(cpath, 'slot')):
            out.append((int(name), cpath))
    out.sort()
    return out


def _component_dev(cpath):
    """Block device backing an enclosure component, or '' when the bay is
    empty. The kernel exposes it as <component>/device/block/<name>."""
    bdir = os.path.join(cpath, 'device', 'block')
    try:
        names = sorted(os.listdir(bdir))
    except OSError:
        return ''
    return names[0] if names else ''


def _lsblk_disks():
    """name -> disk facts, plus a rolled-up 'usage' string from its partitions.

    One lsblk call for the whole machine; the bay map then joins on device
    name. Runs unprivileged (no_sudo) because it needs no privilege.
    """
    out, _, _ = run(['lsblk', '-J', '-b', '-o',
                     'NAME,TYPE,SIZE,MODEL,SERIAL,ROTA,TRAN,FSTYPE,LABEL,MOUNTPOINT'],
                    no_sudo=True)
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return {}
    disks = {}
    for d in data.get('blockdevices') or []:
        if (d.get('type') or '') != 'disk':
            continue
        disks[d.get('name')] = {
            'dev': d.get('name'),
            'size_bytes': d.get('size'),
            'size': _human(d.get('size')),
            'model': (d.get('model') or '').strip(),
            'serial': (d.get('serial') or '').strip(),
            'rotational': bool(d.get('rota')),
            'disk_type': 'HDD' if d.get('rota') else 'SSD',
            'transport': d.get('tran'),
            'partitions': len(d.get('children') or []),
            'usage': _usage(d),
        }
    return disks


def _usage(disk):
    """Human summary of what a disk is being used for, from its partition
    table alone — deliberately cheap. ZFS labels its members, so a pool name
    surfaces here without shelling out to zpool."""
    kids = disk.get('children') or []
    if not kids and not disk.get('fstype'):
        return 'Free'
    parts = []
    for c in kids + [disk]:
        fstype = c.get('fstype') or ''
        if not fstype:
            continue
        if fstype == 'zfs_member':
            label = c.get('label') or ''
            parts.append('ZFS: ' + label if label else 'ZFS member')
        elif fstype == 'linux_raid_member':
            parts.append('MD RAID member')
        elif fstype == 'LVM2_member':
            parts.append('LVM PV')
        elif c.get('mountpoint'):
            parts.append(fstype + ' at ' + c['mountpoint'])
        else:
            parts.append(fstype)
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ', '.join(seen) if seen else 'Free'


def _human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ''
    for unit in ('B', 'K', 'M', 'G', 'T', 'P'):
        if n < 1024 or unit == 'P':
            return ('%d%s' % (n, unit)) if unit == 'B' else ('%.1f%s' % (n, unit))
        n /= 1024
    return ''


def _chassis_info():
    """45Drives chassis identity, when their tooling is installed. Optional:
    absence just means we fall back to raw slot numbering."""
    if not os.path.isfile(SERVER_INFO):
        return None
    try:
        with open(SERVER_INFO, 'r') as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    return {
        'model': info.get('Model'),
        'serial': info.get('Serial'),
        'chassis_size': info.get('Chassis Size'),
        'alias_style': info.get('Alias Style'),
        'motherboard': info.get('Motherboard'),
        'hba': info.get('HBA') or [],
        'source': SERVER_INFO,
    }


def _vdev_labels():
    """dev name -> bay label, from /etc/vdev_id.conf when 45Drives' dmap has
    written one. These are the authoritative labels for a 45Drives chassis, so
    they win over anything we derive; they only cover OCCUPIED bays, because
    the mapping is by-path and an empty path resolves to nothing."""
    if not os.path.isfile(VDEV_ID_CONF):
        return {}
    labels = {}
    try:
        with open(VDEV_ID_CONF, 'r') as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    for line in lines:
        m = RE_VDEV_ALIAS.match(line)
        if not m:
            continue
        bay, by_path = m.group(1), m.group(2)
        try:
            dev = os.path.basename(os.path.realpath(by_path))
        except OSError:
            continue
        if dev and dev != os.path.basename(by_path):
            labels[dev] = bay
    return labels


def _derived_label(profile, index):
    """Position label for a bay when vdev_id.conf cannot supply one.

    index is 0-based within the trimmed slot list. A single-row chassis reads
    '1-N'; a multi-row chassis is filled row-major, which matches how every
    profile in _CHASSIS_PROFILES is physically numbered.
    """
    if not profile:
        return str(index)
    rows = profile.get('rows') or 1
    per_row = max(1, int(round(profile['bays'] / float(rows))))
    row = index // per_row + 1
    col = index % per_row + 1
    return profile['label'].format(n=index + 1, row=row, col=col)


def _enclosure_ids():
    try:
        return sorted(n for n in os.listdir(ENCLOSURE_ROOT)
                      if RE_ENCLOSURE_ID.match(n))
    except OSError:
        return []


def _scan_enclosure(enc_id, disks, vdev, profile):
    """Build one enclosure's bay list from SES plus the joined disk facts."""
    enc_path = os.path.join(ENCLOSURE_ROOT, enc_id)
    comps = _component_dirs(enc_path)

    real, phantom = [], []
    for index, cpath in comps:
        status = _read(os.path.join(cpath, 'status'))
        if status in _PHANTOM_STATUS:
            phantom.append(int(_read(os.path.join(cpath, 'slot'), '-1') or -1))
            continue
        real.append((index, cpath, status))

    # Present bays in physical slot order, not component order — the two differ
    # on this hardware (component 9 is slot 0).
    real.sort(key=lambda t: _slot_num(t[1]))

    # A backplane may advertise more array slots than the chassis has bays.
    # Trim from the end, and report what was dropped rather than hiding it.
    trimmed = []
    if profile and len(real) > profile['bays']:
        for index, cpath, _s in real[profile['bays']:]:
            trimmed.append(_slot_num(cpath))
        real = real[:profile['bays']]

    bays, mismatches = [], []
    for position, item in enumerate(real):
        index, cpath, status = item
        slot = _slot_num(cpath)
        dev = _component_dev(cpath)
        derived = _derived_label(profile, position)
        label, source = derived, ('chassis' if profile else 'ses')
        if dev and dev in vdev:
            label, source = vdev[dev], 'vdev_id.conf'
            if vdev[dev] != derived:
                mismatches.append({'slot': slot, 'vdev': vdev[dev],
                                   'derived': derived})
        bay = {
            'label': label,
            'label_source': source,
            'slot': slot,
            'component': index,
            'position': position,
            'status': status,
            'occupied': bool(dev),
            'dev': dev,
            'locate': _read_flag(os.path.join(cpath, 'locate')),
            'fault': _read_flag(os.path.join(cpath, 'fault')),
            'led_writable': os.path.isfile(os.path.join(cpath, 'locate')),
        }
        if dev:
            bay.update(disks.get(dev) or {})
        bays.append(bay)

    return {
        'id': enc_id,
        'vendor': _read(os.path.join(enc_path, 'device', 'vendor')),
        'model': _read(os.path.join(enc_path, 'device', 'model')),
        'revision': _read(os.path.join(enc_path, 'device', 'rev')),
        'ses_id': _read(os.path.join(enc_path, 'id')),
        'components': _read(os.path.join(enc_path, 'components')),
        'rows': (profile or {}).get('rows', 1),
        'bays': bays,
        'occupied': sum(1 for b in bays if b['occupied']),
        'phantom_slots': sorted(s for s in phantom if s >= 0),
        'trimmed_slots': trimmed,
        'label_mismatches': mismatches,
    }


def _slot_num(cpath):
    try:
        return int(_read(os.path.join(cpath, 'slot'), '-1') or -1)
    except ValueError:
        return -1


# ─── PCIe carrier cards (bifurcated M.2 adapters) ───────────────────────
# A passive M.2 carrier (ASUS Hyper M.2 and friends) has NO switch, no
# controller and no PCI ID — nothing on the card enumerates, so the card itself
# is undetectable and is never named here. What IS detectable is the shape
# bifurcation leaves behind: the motherboard splits one x16 slot into four x4
# root ports at consecutive FUNCTIONS of the same PCI device, and each drive
# hangs off one of them. So drives sharing a parent root-port device are on the
# same card, and the parent's function number is their position on it.

def _pci_chain(dev):
    """(endpoint_bdf, parent_bdf) for a block device, or (None, None).

    Read from the realpath of /sys/block/<dev>: the PCI components of the path
    are the topology, deepest-last. The endpoint is the drive's own controller;
    its parent is the root port (or a switch port, on an active carrier).
    """
    try:
        real = os.path.realpath(os.path.join('/sys/block', dev))
    except OSError:
        return None, None
    bdfs = [p for p in real.split('/') if RE_BDF.match(p)]
    if len(bdfs) < 2:
        return (bdfs[0] if bdfs else None), None
    return bdfs[-1], bdfs[-2]


def _dmi_slot_names():
    """Bus address -> physical slot designation ('PCIE5'), from SMBIOS type 9.

    Optional enrichment: needs a `dmidecode -t slot` sudoers grant (see the
    README). Without it carriers are labelled by their PCI group instead.
    A bifurcated slot is reported ONCE, with the bus address of its FIRST
    function — which is why the lookup below matches any member.
    """
    out, _, rc = run(['dmidecode', '-t', 'slot'])
    if rc != 0 or not out.strip():
        return {}
    slots, designation = {}, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('Designation:'):
            designation = line.split(':', 1)[1].strip()
        elif line.startswith('Bus Address:') and designation:
            addr = line.split(':', 1)[1].strip()
            if RE_BDF.match(addr):
                slots[addr] = designation
    return slots


def _carriers(disks):
    """Group drives by the PCI root-port device their endpoint hangs off.

    A group of 2+ is treated as a carrier card; a lone drive is just a device
    in a slot and is left for the unassigned list. Position within the card is
    the parent's function number, which follows the physical lane groups and
    therefore the sockets in order — stable and monotonic, but which end is
    socket 1 is a card-layout convention nothing reports (see README).
    """
    dmi = _dmi_slot_names()
    groups = {}
    for name in sorted(disks):
        endpoint, parent = _pci_chain(name)
        if not endpoint or not parent:
            continue
        group = parent.rsplit('.', 1)[0]            # domain:bus:dev, no function
        try:
            position = int(parent.rsplit('.', 1)[1], 16)
        except (IndexError, ValueError):
            position = 0
        entry = dict(disks[name])
        entry.update({'position': position, 'pci': endpoint,
                      'root_port': parent,
                      'link_speed': _read('%s/%s/current_link_speed' % (PCI_DEVICES, parent)),
                      'link_width': _read('%s/%s/current_link_width' % (PCI_DEVICES, parent))})
        groups.setdefault(group, []).append(entry)

    out = []
    for group in sorted(groups):
        members = sorted(groups[group], key=lambda m: m['position'])
        if len(members) < 2:                        # not a carrier — one device
            continue
        slot = ''
        for m in members:                           # DMI names the first function
            if m['pci'] in dmi:
                slot = dmi[m['pci']]
                break
        out.append({
            'group': group,
            'slot': slot,
            'label': slot or ('PCIe group ' + group),
            'slot_known': bool(slot),
            'slots': len(members),
            'members': members,
        })
    return out


@bp.route('/api/drive-bays')
def api_drive_bays():
    """Bay map for every SES enclosure, plus the disks that are not in one.

    Deliberately excludes SMART: it spins commands per drive and would make
    the page slow to first paint. /api/drive-bays/health fills that in after.
    """
    ids = _enclosure_ids()
    disks = _lsblk_disks()
    vdev = _vdev_labels()
    chassis = _chassis_info()
    profile = None
    if chassis and chassis.get('chassis_size'):
        profile = _CHASSIS_PROFILES.get(chassis['chassis_size'])

    enclosures = [_scan_enclosure(i, disks, vdev, profile) for i in ids]

    in_bays = set()
    for e in enclosures:
        for b in e['bays']:
            if b['dev']:
                in_bays.add(b['dev'])

    # Drives with no enclosure component may still have a derivable position:
    # a bifurcated M.2 carrier groups them by PCI root port (see _carriers).
    carriers = _carriers({k: v for k, v in disks.items() if k not in in_bays})
    on_carrier = set()
    for c in carriers:
        for m in c['members']:
            on_carrier.add(m['dev'])

    # Whatever is left has no physical position at all — onboard M.2, SATA,
    # USB. Saying so is the honest rendering; inventing a slot would not be.
    unassigned = [disks[name] for name in sorted(disks)
                  if name not in in_bays and name not in on_carrier]

    return jsonify({
        'available': bool(enclosures),
        'reason': None if enclosures else (
            'No SCSI Enclosure Services device found under %s. Bay mapping '
            'needs a backplane or HBA that exposes SES.' % ENCLOSURE_ROOT),
        'chassis': chassis,
        'enclosures': enclosures,
        'carriers': carriers,
        'unassigned': unassigned,
        'led_helper': os.path.isfile(LED_HELPER),
    })


def _smart(dev):
    """Normalized SMART for one device. smartctl's exit status is a bitmask,
    so the JSON is always parsed rather than gated on the return code."""
    if not RE_DEV.match(dev):
        return dev, {'available': False, 'error': 'invalid device'}
    out, e, _ = run(['smartctl', '-H', '-A', '-i', '-j', '/dev/' + dev])
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        data = {}
    if not data:
        return dev, {'available': False, 'error': e.strip() or 'no SMART data'}

    status = data.get('smart_status') or {}
    info = {
        'available': True,
        'health': ('OK' if status['passed'] else 'FAILED')
                  if 'passed' in status else 'unknown',
        'temperature_c': (data.get('temperature') or {}).get('current'),
        'power_on_hours': (data.get('power_on_time') or {}).get('hours'),
        'firmware': data.get('firmware_version'),
        'rotation_rate': data.get('rotation_rate'),
    }
    attrs = {}
    for a in ((data.get('ata_smart_attributes') or {}).get('table') or []):
        attrs[a.get('name')] = (a.get('raw') or {}).get('value')
    if attrs:
        info['reallocated'] = attrs.get('Reallocated_Sector_Ct')
        info['pending'] = attrs.get('Current_Pending_Sector')
        info['uncorrectable'] = attrs.get('Offline_Uncorrectable')
        info['power_cycles'] = attrs.get('Power_Cycle_Count')
        info['start_stop'] = attrs.get('Start_Stop_Count')
    nvme = data.get('nvme_smart_health_information_log')
    if nvme:
        info['power_on_hours'] = info['power_on_hours'] or nvme.get('power_on_hours')
        info['temperature_c'] = info['temperature_c'] or nvme.get('temperature')
        info['media_errors'] = nvme.get('media_errors')
        info['percentage_used'] = nvme.get('percentage_used')
    return dev, info


@bp.route('/api/drive-bays/health')
def api_health():
    """SMART for every disk currently in a bay, gathered concurrently.

    smartctl takes ~0.5-2s per drive and a full chassis can hold 60, so this
    is a separate call the page makes after the map is already on screen. The
    worker cap keeps a big chassis from opening 60 subprocesses at once.
    """
    disks = _lsblk_disks()
    vdev = _vdev_labels()
    chassis = _chassis_info()
    profile = None
    if chassis and chassis.get('chassis_size'):
        profile = _CHASSIS_PROFILES.get(chassis['chassis_size'])
    devs = []
    for enc_id in _enclosure_ids():
        for bay in _scan_enclosure(enc_id, disks, vdev, profile)['bays']:
            if bay['dev']:
                devs.append(bay['dev'])
    if request.args.get('all') == '1':
        in_bays = set(devs)
        devs.extend(n for n in sorted(disks) if n not in in_bays)
    if not devs:
        return jsonify({'health': {}})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_smart, devs))
    return jsonify({'health': dict(results)})


@bp.route('/api/drive-bays/led', methods=['POST'])
def api_led():
    """Drive the real per-slot identify/fault LED on the backplane.

    The sysfs attributes are root-owned, so the write goes through the
    plugin's own helper. Every argument is allowlist-validated here AND again
    in the helper — the helper is a root trust boundary and does not trust
    its caller.
    """
    if not _is_admin():
        return err('Admin required', 403)
    data = request.get_json(silent=True) or {}
    enc = str(data.get('enclosure') or '')
    comp = str(data.get('component') or '')
    attr = str(data.get('attr') or 'locate')
    on = bool(data.get('on'))
    if not RE_ENCLOSURE_ID.match(enc):
        return err('Invalid enclosure id')
    if not RE_COMPONENT.match(comp):
        return err('Invalid component')
    if attr not in ('locate', 'fault'):
        return err('Invalid attribute')
    target = os.path.join(ENCLOSURE_ROOT, enc, comp, attr)
    if not os.path.isfile(target):
        return err('No %s control for that bay' % attr, 404)
    if not os.path.isfile(LED_HELPER):
        return err('LED helper not installed at %s — see the plugin README'
                   % LED_HELPER, 503)
    out, e, rc = run([LED_HELPER, enc, comp, attr, '1' if on else '0'])
    if rc != 0:
        return err(e.strip() or out.strip() or 'LED write failed', 500)
    return jsonify({'success': True, 'enclosure': enc, 'component': comp,
                    'attr': attr, 'on': on})


def _summary():
    """Front-page block: bay occupancy and any drive the backplane flags."""
    try:
        disks = _lsblk_disks()
        vdev = _vdev_labels()
        chassis = _chassis_info()
        profile = None
        if chassis and chassis.get('chassis_size'):
            profile = _CHASSIS_PROFILES.get(chassis['chassis_size'])
        total = occupied = faulted = 0
        for enc_id in _enclosure_ids():
            enc = _scan_enclosure(enc_id, disks, vdev, profile)
            total += len(enc['bays'])
            occupied += enc['occupied']
            faulted += sum(1 for b in enc['bays']
                           if b['fault'] or (b['occupied'] and
                                             b['status'] not in ('OK', '')))
        if not total:
            return {'available': False}
        return {'available': True, 'bays': total, 'occupied': occupied,
                'empty': total - occupied, 'faulted': faulted,
                'chassis': (chassis or {}).get('model')}
    except Exception:                                     # noqa: BLE001
        return {'available': False}


def _alerts():
    """Raise only on a bay the ENCLOSURE itself reports as bad — SMART
    warnings belong to the disks module, and duplicating them here would
    double-alert the same drive."""
    out = []
    try:
        s = _summary()
        if s.get('available') and s.get('faulted'):
            out.append({'key': 'drive-bays-fault',
                        'message': '%d drive bay(s) reporting a fault'
                                   % s['faulted']})
    except Exception:                                     # noqa: BLE001
        pass
    return out


MODULE = {
    'id': 'drive-bays',
    'label': 'Drive Bays',
    'category': 'Storage',
    'version': '1.1',
    'blueprint': bp,
    'nav': {'cat': 'storage', 'cat_order': 20, 'pages': [
        {'id': 'drive-bays', 'label': 'Drive Bays', 'icon': 'grid'}]},
    'assets': {'js': ['plugin.js']},
    'summary': _summary,
    'alerts': _alerts,
}
