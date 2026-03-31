# Multi-Operator Image Manifest Management - Infrastructure Design

**Version:** 2.0
**Date:** 2026-03-26
**Status:** Design Review - Infrastructure Foundation

---

## Table of Contents

1. [Overview](#overview)
2. [Design Goals](#design-goals)
3. [Architecture](#architecture)
4. [Design Decisions](#design-decisions)
5. [Implementation Plan](#implementation-plan)

6. [File Changes](#file-changes)
7. [Migration Strategy](#migration-strategy)
8. [Validation](#validation)
9. [Future Work](#future-work)

---

## Overview

This document describes the infrastructure design for unified image manifest management supporting both **GPU-operator** and **Network-operator**. This foundation will enable:

1. GPU-operator upgrade testing (released → RC) - **future work**
2. Network-operator co-deployment scenarios - **future work**

### Current State

- **Existing manifests:** GPU-operator versions v1.0.0 through v1.4.1 in `tests/pytests/image-manifest/`

- **File naming:** `release_v{version}_{type}_images.yaml`

- **Alternative manifest support:** `--alternative-image-manifest` parameter exists but not used by critical tests

- **Single operator focus:** Only GPU-operator

### Target State (Infrastructure)

- **Multi-operator support:** Both GPU-operator and Network-operator manifests

- **Organized structure:** Subdirectories per operator type

- **Metadata identification:** Explicit operator and version fields in YAML

- **Unified fixture:** `all_image_versions` loads all released manifests

- **Clean interface:** Remove unused `--alternative-image-manifest`

---

## Design Goals

### Primary Goals

1. **Unified manifest management** - Single infrastructure for GPU and Network operators
2. **Clear identification** - Operator type and version explicitly defined
3. **Future-ready** - Foundation for upgrade and co-deployment testing
4. **Maintain compatibility** - No breaking changes to existing tests
5. **Clean organization** - Directory structure reflects operator types

### Non-Goals (Future Work)

- Operator upgrade test implementation

- Network-operator co-deployment test implementation

- Downgrade/rollback testing

- Performance regression testing

---

## Architecture

### Directory Structure

```text
tests/pytests/image-manifest/
├── gpu-operator/
│   ├── v1.4.1_external_images.yaml
│   ├── v1.4.1_internal_images.yaml
│   ├── v1.4.0_external_images.yaml
│   ├── v1.4.0_internal_images.yaml
│   ├── v1.3.1_external_images.yaml
│   ├── v1.3.1_internal_images.yaml
│   ├── v1.3.0_external_images.yaml
│   ├── v1.3.0_internal_images.yaml
│   ├── v1.2.2_external_images.yaml
│   ├── v1.2.1_external_images.yaml
│   ├── v1.2.0_external_images.yaml
│   ├── v1.1.0_external_images.yaml
│   ├── v1.0.0_external_images.yaml
│   └── ... (internal manifests for each version)
│
└── network-operator/
    ├── v1.0.0_external_images.yaml (placeholder)
    ├── v1.0.0_internal_images.yaml (placeholder)
    └── ... (to be populated)

```

### Manifest Metadata Structure

Each YAML manifest includes metadata for identification and validation:

```yaml
# Example: tests/pytests/image-manifest/gpu-operator/v1.4.1_external_images.yaml
images:
  meta:
    operator: gpu-operator    # NEW FIELD - operator identification
    version: v1.4.1           # NEW FIELD - version identification
    registry:
      default: docker.io
      mirror:
        enable: yes
        url: registry.test.pensando.io:5000

  k8:
    gpu-operator:
      location: repo://rocm.github.io/gpu-operator:gpu-operator-charts
      version: v1.4.1
      kind: helm-chart

    gpu-controller-manager:
      key: controllerManager.manager.image
      location: container://<registry>/rocm/gpu-operator
      version: v1.4.1
      kind: container

    # ... rest of components

```

```yaml
# Example: tests/pytests/image-manifest/network-operator/v1.0.0_external_images.yaml
images:
  meta:
    operator: network-operator  # NEW FIELD
    version: v1.0.0              # NEW FIELD
    registry:
      default: docker.io

  k8:
    network-operator:
      location: repo://amd.io/network-operator:network-operator-charts
      version: v1.0.0
      kind: helm-chart

    # ... network-operator components (to be populated)

```

### Fixture Architecture

```text
tests/pytests/
├── conftest.py (MODIFIED)
│   ├── all_image_versions fixture (NEW)
│   │   └── Scans image-manifest/{operator}/*_external_images.yaml
│   │   └── Returns: dict[operator_type][version] => image_info
│   │
│   ├── images fixture (existing, ENHANCED)
│   │   └── Loads RC manifest from --image-manifest
│   │   └── Optional validation of metadata fields
│   │
│   └── alternative_images fixture (REMOVE)
│       └── No longer needed

```

### all_image_versions Fixture Implementation

```python
@pytest.fixture(scope="session")
def all_image_versions(request, environment):
    """
    Load all released operator image manifests.

    Scans tests/pytests/image-manifest/{operator}/ subdirectories
    and loads all *_external_images.yaml files.

    Returns:
        dict[str, dict[str, dict]]: Nested structure
        {
            "gpu-operator": {
                "v1.4.1": {image_info_dict},
                "v1.4.0": {image_info_dict},
                ...
            },
            "network-operator": {
                "v1.0.0": {image_info_dict},
                ...
            }
        }

    Validation:
        - Operator from directory name matches images.meta.operator
        - Version from filename matches images.meta.version
        - Warns on mismatch but doesn't fail (allows transition)

    """
    from pathlib import Path
    from ruamel.yaml import YAML
    import logging

    logger = logging.getLogger("conftest.all_image_versions")
    yaml = YAML()
    yaml.preserve_quotes = True

    manifest_base_dir = Path(__file__).parent / "image-manifest"
    version_map = {}

    # Iterate through operator subdirectories
    for operator_dir in manifest_base_dir.iterdir():
        if not operator_dir.is_dir():
            continue

        operator_type = operator_dir.name  # "gpu-operator" or "network-operator"
        version_map[operator_type] = {}

        # Load all *_external_images.yaml files in this operator directory
        for manifest_file in operator_dir.glob("*_external_images.yaml"):
            try:
                # Extract version from filename: v1.4.1_external_images.yaml -> v1.4.1
                filename_version = manifest_file.stem.replace("_external_images", "")

                # Load manifest
                manifest_data = yaml.load(manifest_file)

                # Validate metadata (optional - warn on mismatch)
                if 'meta' in manifest_data['images']:
                    meta = manifest_data['images']['meta']

                    # Check operator field
                    if 'operator' in meta:
                        if meta['operator'] != operator_type:
                            logger.warning(
                                f"{manifest_file}: metadata operator '{meta['operator']}' "
                                f"doesn't match directory '{operator_type}'"
                            )
                    else:
                        logger.warning(f"{manifest_file}: missing 'operator' field in metadata")

                    # Check version field
                    if 'version' in meta:
                        if meta['version'] != filename_version:
                            logger.warning(
                                f"{manifest_file}: metadata version '{meta['version']}' "
                                f"doesn't match filename '{filename_version}'"
                            )
                    else:
                        logger.warning(f"{manifest_file}: missing 'version' field in metadata")
                else:
                    logger.warning(f"{manifest_file}: missing 'meta' section")

                # Build image_info using existing helper
                image_info = _build_image_info(environment, manifest_data['images'])

                # Store in nested dict
                version_map[operator_type][filename_version] = image_info

            except Exception as e:
                logger.error(f"Failed to load {manifest_file}: {e}")
                continue

    # Log summary
    for op_type, versions in version_map.items():
        logger.info(f"Loaded {len(versions)} versions for {op_type}: {sorted(versions.keys())}")

    return version_map

```

### Enhanced images Fixture (Optional Validation)

```python
@pytest.fixture(scope="session")
def images(request, gpu_cluster, environment):
    """
    Existing fixture - loads RC manifest from --image-manifest

    ENHANCEMENT: Add optional metadata validation
    """
    # ... existing code to load manifest ...

    image_manifest = dict(yaml.load(file_obj))

    # NEW: Optional metadata validation
    if 'meta' in image_manifest['images']:
        meta = image_manifest['images']['meta']
        if 'operator' not in meta:
            Logger.warning(f"Image manifest missing 'operator' field in metadata")
        if 'version' not in meta:
            Logger.warning(f"Image manifest missing 'version' field in metadata")
    else:
        Logger.warning(f"Image manifest missing 'meta' section")

    # ... rest of existing code ...

    return image_info

```

---

## Design Decisions

### Decision Log

| ID | Question | Decision | Rationale |
| --- | --- | --- | --- |
| D1 | Image manifest selection | Load `*_external_images.yaml` only | External images are public releases |
| D2 | Version extraction | Read from `images.meta.version` field | Explicit version in manifest, validate against filename |
| D3 | RC manifest location | Passed via `--image-manifest` CLI | RC built by CI, passed as parameter |
| D4 | all_image_versions dependencies | No `gpu_cluster` dependency | Fixture only loads manifests, doesn't interact with cluster |
| D5 | Alternative manifest removal | Remove `--alternative-image-manifest` | Not used by critical tests, will be manually reviewed/removed |
| D6 | Operator identification | Add `images.meta.operator` field | Explicit operator type, validates against directory structure |
| D7 | Version format | Use `v1.4.1` (with 'v' prefix) | Matches Helm chart version convention |
| D8 | Fixture structure | Nested dict: `[operator][version]` | Clean separation, intuitive access |
| D9 | Directory organization | Subdirectories per operator | Clear organization, scales to multiple operators |
| D10 | File naming | `v{version}_{type}_images.yaml` | Simple, clean, version immediately visible |
| D11 | Metadata validation | Optional warning (don't fail) | Allows gradual migration, helps catch errors |
| D12 | Network-operator manifests | Load if exist (create placeholder) | Future-ready infrastructure |
| D13 | Internal manifests | Update both external and internal | Consistency across all manifest files |
| D14 | Migration strategy | One-time script execution | Clean break, complete migration |

---

## Implementation Plan

### Phase 1: Preparation (Week 1, Days 1-2)

#### Task 1.1: Create Migration Script

**File:** `ci-internal/migrate_image_manifests.py`

**Purpose:** Automate migration of existing manifests to new structure

**Features:**

- Create operator subdirectories

- Rename and move files

- Add metadata fields to YAML

- Validate migration

**Script outline:**

```python
#!/usr/bin/env python3
"""
Migrate image manifests to new multi-operator structure.

Usage:
    python ci-internal/migrate_image_manifests.py --dry-run
    python ci-internal/migrate_image_manifests.py --execute
"""
import re
from pathlib import Path
from ruamel.yaml import YAML

def migrate_manifests(dry_run=True):
    """
    Migrate manifests from:
        tests/pytests/image-manifest/release_v1.4.1_external_images.yaml
    To:
        tests/pytests/image-manifest/gpu-operator/v1.4.1_external_images.yaml

    Also migrates:
        ci-internal/release_v1.4.1_internal_images.yaml
    """
    # Patterns to migrate
    locations = [
        {
            "source_dir": Path("tests/pytests/image-manifest"),
            "pattern": r"release_v(\d+\.\d+\.\d+)_(external|internal)_images\.yaml",
            "target_subdir": "gpu-operator",
        },
        {
            "source_dir": Path("ci-internal"),
            "pattern": r"release_v(\d+\.\d+\.\d+)_internal_images\.yaml",
            "target_subdir": "gpu-operator",
        }
    ]

    yaml = YAML()
    yaml.preserve_quotes = True

    for location in locations:
        source_dir = location["source_dir"]
        pattern = location["pattern"]
        target_subdir = location["target_subdir"]

        # Create target directory
        target_dir = source_dir / target_subdir
        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)

        # Find matching files
        for old_file in source_dir.glob("release_*.yaml"):
            match = re.match(pattern, old_file.name)
            if not match:
                continue

            version = match.group(1)
            image_type = match.group(2) if len(match.groups()) > 1 else "internal"

            # New filename and path
            new_filename = f"v{version}_{image_type}_images.yaml"
            new_file = target_dir / new_filename

            # Load manifest
            manifest = yaml.load(old_file)

            # Add metadata
            if 'meta' not in manifest['images']:
                manifest['images']['meta'] = {}

            manifest['images']['meta']['operator'] = target_subdir
            manifest['images']['meta']['version'] = f'v{version}'

            # Execute or dry-run
            if dry_run:
                print(f"[DRY-RUN] Would migrate:")
                print(f"  From: {old_file}")
                print(f"  To:   {new_file}")
                print(f"  Metadata: operator={target_subdir}, version=v{version}")
            else:
                yaml.dump(manifest, new_file)
                print(f"Migrated: {old_file.name} → {new_file}")
                # Optionally remove old file
                # old_file.unlink()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--execute", action="store_true", help="Actually perform migration")
    args = parser.parse_args()

    if args.execute:
        migrate_manifests(dry_run=False)
    else:
        migrate_manifests(dry_run=True)

```

#### Task 1.2: Update Seed Manifests in ci-internal

**Files to update:**

- `ci-internal/sanity-images.yml`

- `ci-internal/sandbox-sanity-images.yml`

**Changes:** Add metadata fields to `images.meta` section

#### Task 1.3: Create Network-Operator Placeholder

**File:** `tests/pytests/image-manifest/network-operator/v1.0.0_external_images.yaml`

**Content:**

```yaml
---

images:
  meta:
    operator: network-operator
    version: v1.0.0
    registry:
      default: docker.io

  k8:
    # TODO: Populate with actual network-operator components
    # Reference: https://instinct.docs.amd.com/projects/network-operator/

```

### Phase 2: Implementation (Week 1, Days 3-5)

#### Task 2.1: Implement all_image_versions Fixture

**File:** `tests/pytests/conftest.py`

**Changes:**

- Add `all_image_versions` fixture (implementation shown above)

- Keep existing `images` fixture

- Add optional metadata validation to `images` fixture

- Remove `alternative_images` fixture

- Update `pytest_addoption` to remove `--alternative-image-manifest`

#### Task 2.2: Update k8_jobd_ctl.py (RC Manifest Generation)

**File:** `ci-internal/k8_jobd_ctl.py`

**Changes:** Ensure generated RC manifests include metadata

```python
def generate_rc_manifest(seed_manifest, output_path, rc_version):
    """
    Generate RC manifest from seed, ensuring metadata is present
    """
    # Load seed manifest
    manifest = load_yaml(seed_manifest)

    # Add/update metadata
    if 'meta' not in manifest['images']:
        manifest['images']['meta'] = {}

    # Infer operator from seed filename or set explicitly
    manifest['images']['meta']['operator'] = 'gpu-operator'  # or from config
    manifest['images']['meta']['version'] = rc_version

    # ... rest of RC generation logic ...

    save_yaml(manifest, output_path)

```

#### Task 2.3: Remove --alternative-image-manifest

**File:** `tests/pytests/k8_test_launcher.sh`

**Changes:**

- Remove `--alt-image-manifest` parameter parsing

- Remove `ALT_IMAGE_MANIFEST` variable

- Remove passing `--alternative-image-manifest` to pytest

### Phase 3: Migration Execution (Week 2, Day 1)

#### Task 3.1: Run Migration Script

```bash
# Test with dry-run
python ci-internal/migrate_image_manifests.py --dry-run

# Review output, then execute
python ci-internal/migrate_image_manifests.py --execute

```

#### Task 3.2: Verify Migration

- Check all files moved to subdirectories

- Validate metadata added correctly

- Test loading with new fixture

#### Task 3.3: Cleanup Old Files (Optional)

- Remove old `release_v*.yaml` files after confirming migration

### Phase 4: Testing & Validation (Week 2, Days 2-3)

#### Task 4.1: Unit Test for Fixture

**File:** `tests/pytests/test_conftest.py` (new or existing)

```python
def test_all_image_versions_fixture(all_image_versions):
    """Test all_image_versions fixture loads correctly"""

    # Should have gpu-operator
    assert "gpu-operator" in all_image_versions

    # Should have at least one version
    assert len(all_image_versions["gpu-operator"]) > 0

    # Check version format
    for version in all_image_versions["gpu-operator"].keys():
        assert version.startswith("v")

    # Network-operator may be empty (placeholder only)
    if "network-operator" in all_image_versions:
        # If present, should have v1.0.0
        if len(all_image_versions["network-operator"]) > 0:
            assert "v1.0.0" in all_image_versions["network-operator"]

```

#### Task 4.2: Integration Test

```bash
# Run existing tests to ensure no regression
cd tests/pytests
./k8_test_launcher.sh --app gpu-operator \
    --image-manifest image-manifest/gpu-operator/v1.4.1_external_images.yaml \
    --module gpu_operator


```

#### Task 4.3: Validate RC Manifest Generation

- Trigger CI build

- Check generated RC manifest has metadata

- Verify tests can load RC manifest

---

## File Changes

### Files to Create

```text
tests/pytests/
├── image-manifest/
│   ├── gpu-operator/ (NEW DIRECTORY)
│   └── network-operator/ (NEW DIRECTORY)
│       └── v1.0.0_external_images.yaml (placeholder)
│
ci-internal/
└── migrate_image_manifests.py (NEW SCRIPT)

```

### Files to Modify

```text
tests/pytests/
├── conftest.py
│   └── ADD: all_image_versions fixture
│   └── ENHANCE: images fixture with optional validation
│   └── REMOVE: alternative_images fixture
│
├── k8_test_launcher.sh
│   └── REMOVE: --alt-image-manifest parameter
│
├── image-manifest/ (ALL EXISTING FILES)
│   ├── MOVE: release_*.yaml → gpu-operator/*.yaml
│   └── RENAME: release_v{version}_{type}.yaml → v{version}_{type}.yaml
│   └── ADD: metadata fields to all files
│
ci-internal/
├── sanity-images.yml
│   └── ADD: images.meta.operator and images.meta.version
│
├── sandbox-sanity-images.yml
│   └── ADD: images.meta.operator and images.meta.version
│
├── release_v1.4.0_internal_images.yaml
│   └── MOVE: → image-manifest/gpu-operator/v1.4.0_internal_images.yaml
│   └── ADD: metadata fields
│
├── release_v1.4.1_internal_images.yaml
│   └── MOVE: → image-manifest/gpu-operator/v1.4.1_internal_images.yaml
│   └── ADD: metadata fields
│
└── k8_jobd_ctl.py
    └── ENHANCE: Add metadata when generating RC manifests

```

### Files to Remove (Optional - After Migration Confirmed)

```text
tests/pytests/image-manifest/
└── release_v*.yaml (all old files after migration)

ci-internal/
└── release_v*.yaml (after moving to image-manifest/gpu-operator/)

```

---

## Migration Strategy

### Migration Approach

**One-time automated migration using script:**

1. Create migration script with dry-run mode
2. Test migration with dry-run
3. Execute migration
4. Validate results
5. Remove old files (optional, can keep for rollback)

### Migration Checklist

- [ ] Create `ci-internal/migrate_image_manifests.py`

- [ ] Create operator subdirectories

- [ ] Run migration in dry-run mode

- [ ] Review dry-run output

- [ ] Execute migration

- [ ] Validate all files moved correctly

- [ ] Validate metadata added correctly

- [ ] Test `all_image_versions` fixture loads successfully

- [ ] Run existing tests to ensure no regression

- [ ] Update CI jobs to use new file paths (if needed)

- [ ] Document migration in commit message

- [ ] Optionally remove old files

### Rollback Plan

If migration causes issues:

1. Revert commit
2. Old file structure still intact (if not deleted)
3. Tests continue to work with old structure

---

## Validation

### Metadata Validation Rules

**Directory vs Metadata:**

- Operator from directory name (`gpu-operator/`) should match `images.meta.operator`

- Version from filename (`v1.4.1_external`) should match `images.meta.version`

**Validation Behavior:**

- **all_image_versions fixture:** Warn on mismatch, continue loading

- **images fixture:** Warn if metadata missing, continue loading

**Why optional validation?**

- Allows gradual transition

- Doesn't break existing workflows

- Helps catch configuration errors without failing tests

### Example Validation

```yaml
# File: tests/pytests/image-manifest/gpu-operator/v1.4.1_external_images.yaml
images:
  meta:
    operator: gpu-operator  # Must match directory name
    version: v1.4.1         # Must match filename
    # ...

```

**Fixture validation:**

```python
# Extract from directory and filename
operator_from_dir = "gpu-operator"
version_from_file = "v1.4.1"

# Read from metadata
operator_from_meta = manifest['images']['meta']['operator']  # "gpu-operator"
version_from_meta = manifest['images']['meta']['version']    # "v1.4.1"

# Validate
assert operator_from_dir == operator_from_meta  # Pass
assert version_from_file == version_from_meta   # Pass

```

---

## Future Work

This infrastructure enables future test scenarios:

### 1. GPU-Operator Upgrade Testing (Separate Design)

**Scope:**

- Test upgrade from released version → RC version

- Test operand upgrades (device-plugin, config-manager, etc.)

- Validate state preservation during upgrades

**Usage of this infrastructure:**

```python
def test_gpu_operator_upgrade(all_image_versions, images):
    # Get base version from released manifests
    base = all_image_versions["gpu-operator"]["v1.4.1"]

    # Get RC version from --image-manifest
    rc = images

    # Perform upgrade test
    install_operator(base)
    upgrade_operator(rc)
    validate()

```

### 2. Network-Operator Co-Deployment Testing (Separate Design)

**Scope:**

- Deploy GPU-operator + Network-operator together

- Validate no conflicts

- Test GPU workloads with network policies

**Usage of this infrastructure:**

```python
def test_gpu_network_coexist(all_image_versions):
    # Deploy both operators
    gpu = all_image_versions["gpu-operator"]["v1.4.1"]
    network = all_image_versions["network-operator"]["v1.0.0"]

    install_gpu_operator(gpu)
    install_network_operator(network)

    validate_coexistence()

```

### 3. Multi-Operator Upgrade (Future)

**Scope:**

- Upgrade GPU-operator while Network-operator deployed

- Upgrade both operators independently

- Validate mixed-version compatibility

---

## References

### External Documentation

- AMD Network-Operator: https://instinct.docs.amd.com/projects/network-operator/en/main/index.html

### Internal Code References

- Existing image fixture: `tests/pytests/conftest.py::images()`

- Existing helper: `tests/pytests/conftest.py::_build_image_info()`

- Image processing: `ci-internal/k8_jobd_ctl.py`

- Test launcher: `tests/pytests/k8_test_launcher.sh`

### Related Files

- `tests/pytests/lib/helm_util.py` - Helm operations

- `tests/pytests/lib/k8_util.py` - Kubernetes utilities

- `tests/pytests/lib/spec_util.py` - DeviceConfig utilities

---

## Appendix

### Glossary

- **Operator:** Kubernetes operator (GPU-operator or Network-operator)

- **Operand:** Components deployed by the operator (device-plugin, config-manager, etc.)

- **RC:** Release Candidate - pre-release version for testing

- **Seed/Template Manifest:** Files in ci-internal/ used to generate RC manifests

- **Released Manifest:** Files in tests/pytests/image-manifest/ for released versions

- **Image Info:** Dictionary structure containing image locations and versions

### Document History

| Version | Date | Changes |
| --- | --- | --- |
| 1.0 | 2026-03-26 | Initial design (upgrade-focused) |
| 2.0 | 2026-03-26 | Updated to infrastructure-focused design with multi-operator support |

---

## End of Document
