#!/usr/bin/env python3
import os
import sys
import re
import urllib.request
import urllib.error
import json

# No external dependencies (completely standard library)

# Regex to parse semantic version parts
SEMVER_REGEX = re.compile(r'^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:[-+.](.*))?$')

def parse_version(tag):
    """
    Parses a version tag into a sortable tuple.
    Returns (major, minor, patch, build_number, suffix, original_tag).
    """
    match = SEMVER_REGEX.match(tag)
    if not match:
        # For non-semver tags (e.g. date-based tags), return them as string sort fallback
        return (-1, -1, -1, -1, tag, tag)
    
    parts = match.groups()
    major = int(parts[0])
    minor = int(parts[1]) if parts[1] is not None else 0
    patch = int(parts[2]) if parts[2] is not None else 0
    build = int(parts[3]) if parts[3] is not None else 0
    suffix = parts[4] if parts[4] is not None else ""
    return (major, minor, patch, build, suffix, tag)

def is_prerelease(suffix):
    """
    Detects if a suffix represents a pre-release version or CI/nightly build
    (e.g., rc, beta, alpha, dev, or long numeric build identifiers).
    """
    if not suffix:
        return False
    # If the suffix is purely numeric and has length >= 6 (e.g. 25893932881 or 20260602)
    if suffix.isdigit() and len(suffix) >= 6:
        return True
    
    # Split the suffix by non-alphanumeric characters (hyphens, dots, underscores)
    parts = re.split(r'[^a-zA-Z0-9]', suffix.lower())
    prerelease_keywords = {'rc', 'beta', 'alpha', 'dev', 'canary', 'nightly', 'pre', 'next', 'snapshot', 'unstable'}
    for part in parts:
        if part in prerelease_keywords:
            return True
        if re.match(r'^(?:rc|beta|alpha|dev|pre)\d+$', part):
            return True
    return False

def is_same_flavor(current_parsed, candidate_tag):
    """
    Checks if candidate_tag has the same flavor/suffix suffix pattern as current_parsed.
    For example:
      - '9.1.0-alpine' flavor is 'alpine'. Only tags with '-alpine' are candidates.
      - 'v3.7.1' has no suffix. Only tags without suffix are candidates.
    """
    _, _, _, _, current_suffix, _ = current_parsed
    candidate_parsed = parse_version(candidate_tag)
    _, _, _, _, candidate_suffix, _ = candidate_parsed
    
    # If the current tag is not a pre-release, do not suggest a pre-release candidate
    if not is_prerelease(current_suffix) and is_prerelease(candidate_suffix):
        return False
    
    # Normalize suffixes by lowercase and stripping digits (e.g. 'alpine3.23' -> 'alpine')
    def normalize_suffix(s):
        if not s:
            return ""
        s = s.lower()
        # Remove numbers and dots to match generic flavors like 'alpine', 'slim'
        s = re.sub(r'[\d\.]', '', s)
        return s
    
    return normalize_suffix(current_suffix) == normalize_suffix(candidate_suffix)

def get_docker_hub_tags(namespace, image):
    url = f"https://registry.hub.docker.com/v2/repositories/{namespace}/{image}/tags?page_size=100"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            return [tag['name'] for tag in data.get('results', [])]
    except Exception as e:
        return []

def get_ghcr_tags(image_name):
    token_url = f"https://ghcr.io/token?scope=repository:{image_name}:pull"
    try:
        req = urllib.request.Request(token_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            token_data = json.loads(response.read().decode())
            token = token_data.get('token')
            
        tags_url = f"https://ghcr.io/v2/{image_name}/tags/list"
        req_tags = urllib.request.Request(tags_url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Authorization': f"Bearer {token}"
        })
        with urllib.request.urlopen(req_tags, timeout=5) as response:
            tags_data = json.loads(response.read().decode())
            return tags_data.get('tags', [])
    except Exception as e:
        return []

def get_quay_tags(image_name):
    url = f"https://quay.io/api/v1/repository/{image_name}/tag/"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            return [t['name'] for t in data.get('tags', []) if t.get('is_valid', True)]
    except Exception as e:
        return []

def get_latest_version(image_str):
    parts = image_str.split(':')
    if len(parts) != 2:
        return None, "Invalid format"
    
    full_name, current_tag = parts
    current_parsed = parse_version(current_tag)
    
    name_parts = full_name.split('/')
    
    registry = "docker.io"
    namespace = "library"
    image = ""
    
    if len(name_parts) == 3:
        registry = name_parts[0]
        namespace = name_parts[1]
        image = name_parts[2]
    elif len(name_parts) == 2:
        if '.' in name_parts[0] or ':' in name_parts[0]:
            registry = name_parts[0]
            namespace = ""
            image = name_parts[1]
        else:
            namespace = name_parts[0]
            image = name_parts[1]
    else:
        image = name_parts[0]
        
    tags = []
    if registry == "docker.io":
        tags = get_docker_hub_tags(namespace, image)
    elif registry == "ghcr.io":
        image_name = f"{namespace}/{image}" if namespace else image
        tags = get_ghcr_tags(image_name)
    elif registry == "quay.io":
        image_name = f"{namespace}/{image}" if namespace else image
        tags = get_quay_tags(image_name)
    else:
        return None, f"Unsupported registry ({registry})"
        
    if not tags:
        return None, "No tags found (API error or private repo)"
        
    candidates = []
    for t in tags:
        if is_same_flavor(current_parsed, t):
            candidates.append(parse_version(t))
            
    if not candidates:
        return None, "No matching flavor tags found"
        
    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    
    latest_parsed = candidates[0]
    latest_tag = latest_parsed[5]
    
    is_newer = (latest_parsed[0] > current_parsed[0]) or \
               (latest_parsed[0] == current_parsed[0] and latest_parsed[1] > current_parsed[1]) or \
               (latest_parsed[0] == current_parsed[0] and latest_parsed[1] == current_parsed[1] and latest_parsed[2] > current_parsed[2]) or \
               (latest_parsed[0] == current_parsed[0] and latest_parsed[1] == current_parsed[1] and latest_parsed[2] == current_parsed[2] and latest_parsed[3] > current_parsed[3])
               
    if is_newer:
        return latest_tag, None
    return current_tag, None

def scan_compose_files():
    images = {}
    pattern = re.compile(r'^docker-compose-.*\.yaml$')
    
    # Regex to match image definitions, e.g. "image: mariadb:12.3.2"
    image_regex = re.compile(r'^\s*image:\s*[\'"]?([^\s\'"]+)[\'"]?')
    
    for file in sorted(os.listdir('.')):
        if not pattern.match(file) and file != 'docker-compose.yaml':
            continue
        
        try:
            with open(file, 'r', encoding='utf-8') as f:
                for line in f:
                    # Strip comment portion
                    clean_line = line.split('#')[0].strip()
                    if not clean_line:
                        continue
                    
                    match = image_regex.match(clean_line)
                    if match:
                        img = match.group(1)
                        if ':' in img and '${' not in img:
                            if img not in images:
                                images[img] = []
                            images[img].append(file)
        except Exception as e:
            print(f"⚠️  Error parsing {file}: {e}", file=sys.stderr)
            
    return images

def apply_updates(image_updates):
    if not image_updates:
        print("No updates to apply.")
        return
        
    print("\n✍️  Applying updates to configuration files...")
    
    pattern = re.compile(r'^docker-compose-.*\.yaml$')
    compose_files = sorted([f for f in os.listdir('.') if (pattern.match(f) or f == 'docker-compose.yaml')])
    
    for file in compose_files:
        try:
            with open(file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            original_content = content
            for old_img, new_img in image_updates.items():
                escaped_old = re.escape(old_img)
                img_pattern = re.compile(rf'(image:\s*[\'"]?){escaped_old}([\'"]?)')
                content = img_pattern.sub(rf'\1{new_img}\2', content)
            
            if content != original_content:
                with open(file, 'w', encoding='utf-8') as f:
                    f.write(content)
                print(f"  ✅ Updated Docker images in {file}")
        except Exception as e:
            print(f"  ❌ Error updating {file}: {e}", file=sys.stderr)
            
    print("🎉 All updates applied successfully!")

def main():
    print("🔍 Scanning Compose files for Docker images...")
    images = scan_compose_files()
    
    if not images:
        print("❌ No images found in docker-compose files.")
        sys.exit(1)
        
    print(f"📊 Found {len(images)} unique Docker images. Querying registries...")
    print("=" * 105)
    print(f"{'IMAGE':<45} | {'CURRENT':<15} | {'LATEST':<15} | {'STATUS':<20}")
    print("=" * 105)
    
    updates_available = 0
    errors = 0
    image_updates_dict = {}
    
    for img_str, locations in sorted(images.items()):
        current_tag = img_str.split(':')[1]
        image_name = img_str.split(':')[0]
        
        display_name = image_name
        if len(display_name) > 43:
            display_name = display_name[:40] + "..."
            
        print(f"{display_name:<45} | {current_tag:<15} | ", end="", flush=True)
        
        latest_tag, error = get_latest_version(img_str)
        
        if error:
            print(f"{'Unknown':<15} | ⚠️  {error}")
            errors += 1
        elif latest_tag == current_tag:
            print(f"{latest_tag:<15} | 🟢 Up-to-date")
        else:
            print(f"{latest_tag:<15} | 🔴 Update Available!")
            updates_available += 1
            image_updates_dict[img_str] = f"{image_name}:{latest_tag}"
            
    print("=" * 105)
    print()
    
    print(f"✅ Finished check. {updates_available} updates available, {errors} errors/skips.")
    
    if updates_available > 0:
        print()
        try:
            choice = input("❓ Do you want to update all images to their latest versions in the configuration files? [y/N]: ").strip().lower()
            if choice in ['y', 'yes']:
                apply_updates(image_updates_dict)
            else:
                print("Update cancelled.")
        except (KeyboardInterrupt, EOFError):
            print("\nUpdate cancelled.")

if __name__ == "__main__":
    main()
