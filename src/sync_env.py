#!/usr/bin/env python3
import os
import sys
import secrets

DIST_FILE = ".env.dist"
ENV_FILE = ".env"

def generate_secret():
    return secrets.token_hex(16)

def load_env_file(path):
    """Returns dict {KEY: VALUE}."""
    d = {}
    if not os.path.exists(path):
        return d
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            d[k.strip()] = v.strip()
    return d

def is_sensitive(key):
    k = key.lower()
    return any(x in k for x in ['pass', 'key', 'token', 'secret']) and 'expiry' not in k

def is_telegram(key):
    return 'tg_' in key.lower() or 'telegram' in key.lower()

def main():
    if not os.path.exists(DIST_FILE):
        print(f"Error: {DIST_FILE} not found.")
        sys.exit(1)

    current_vars = load_env_file(ENV_FILE)
    
    # 1. READ DIST AND PREPARE MERGED STRUCTURE
    # We build a list of (type, content) items
    # type: 'comment', 'active_var'
    merged_lines = [] 
    processed_keys = set()
    
    with open(DIST_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            raw_line = line.strip()
            if not raw_line or raw_line.startswith('#') or '=' not in raw_line:
                merged_lines.append(('comment', line))
                continue
            
            # It's a variable definition: KEY=DEFAULT
            key, default_val = line.split('=', 1)
            key = key.strip()
            default_val = default_val.strip()
            
            processed_keys.add(key)
            
            # Use current value if exists, else default from dist
            val_to_use = current_vars.get(key, default_val)
            
            # Store tuple ('active_var', key, val)
            # We don't store "line" because we reconstruct it later
            merged_lines.append(('active_var', key, val_to_use))

    # 2. IDENTIFY OBSOLETE VARIABLES
    obsolete_keys = [k for k in current_vars if k not in processed_keys]
    
    # 3. INTERACTIVE WIZARD
    final_lines = []
    print(f"\nStarting configuration wizard for {ENV_FILE}...\n")
    
    # Iterate through the merged structure (Active vars)
    for item in merged_lines:
        if item[0] == 'comment':
            final_lines.append(item[1])
            continue
            
        # item is ('active_var', key, val)
        _, key, current_val = item
        
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"  Variable: {key}")
        print(f"  Current:  {current_val}")
        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        
        new_val = current_val

        # Logic for sensitive/telegram variables
        if is_sensitive(key):
            if is_telegram(key):
                print("  [k] Keep current value")
                print("  [m] Enter manually")
                choice = input("  Choice [k]: ").strip().lower()
                if choice == 'm':
                    inp = input("  Enter new value: ").strip()
                    if inp:
                        new_val = inp
                        print("  -> Updated")
                    else:
                        print("  -> Kept existing")
                else:
                    print("  -> Kept existing")
            else:
                print("  [k] Keep current value")
                print("  [g] Generate new random value")
                print("  [m] Enter manually")
                choice = input("  Choice [k]: ").strip().lower()
                
                if choice == 'g':
                    new_val = generate_secret()
                    print(f"  -> Generated: {new_val}")
                elif choice == 'm':
                    inp = input("  Enter new value: ").strip()
                    if inp:
                        new_val = inp
                        print("  -> Updated")
                    else:
                        print("  -> Kept existing")
                else:
                    print("  -> Kept existing")
        else:
            # Regular variable
            inp = input("  Press Enter to keep, or type new value: ").strip()
            if inp:
                new_val = inp
                print(f"  -> Updated to: {new_val}")
            else:
                print("  -> Kept existing")
        
        final_lines.append(f"{key}={new_val}\n")
        print("")

    # 4. APPEND OBSOLETE VARIABLES
    if obsolete_keys:
        final_lines.append("\n# --- Obsolete Variables ---\n")
        final_lines.append("# These variables were in .env but are not in .env.dist\n")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print("  Moving obsolete variables to end of file...")
        print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for k in obsolete_keys:
            val = current_vars[k]
            final_lines.append(f"{k}={val}\n")
            print(f"  -> Moved: {k}")
        print("")

    # 5. WRITE TO FILE
    with open(ENV_FILE, 'w', encoding='utf-8') as f:
        f.writelines(final_lines)
    
    os.chmod(ENV_FILE, 0o600)
    print(f"SUCCESS! {ENV_FILE} has been updated.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)
