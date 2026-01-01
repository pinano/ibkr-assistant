#!/bin/bash
set -u

DIST_FILE=".env.dist"
ENV_FILE=".env"
TEMP_FILE=".env.tmp"

# Check if .env.dist exists
if [ ! -f "$DIST_FILE" ]; then
    echo "Error: $DIST_FILE not found."
    exit 1
fi

# Helper to generate secure random hex string
generate_secret() {
    if command -v openssl &> /dev/null; then
        openssl rand -hex 16
    else
        # Fallback if openssl not available
        head -c 16 /dev/urandom | xxd -p
    fi
}

# Helper to check if a variable is a password/secret type
is_sensitive_var() {
    local key="$1"
    local lower_key
    lower_key=$(echo "$key" | tr '[:upper:]' '[:lower:]')
    
    if ([[ "$lower_key" == *"pass"* ]] || \
        [[ "$lower_key" == *"key"* ]] || \
        [[ "$lower_key" == *"token"* ]] || \
        [[ "$lower_key" == *"secret"* ]]) && \
       [[ "$lower_key" != *"expiry"* ]]; then
        return 0  # true
    fi
    return 1  # false
}

# Helper to check if a variable is Telegram-related
is_telegram_var() {
    local key="$1"
    local lower_key
    lower_key=$(echo "$key" | tr '[:upper:]' '[:lower:]')
    
    if [[ "$lower_key" == *"tg_"* ]] || [[ "$lower_key" == *"telegram"* ]]; then
        return 0  # true
    fi
    return 1  # false
}

# Check if .env file exists (to determine mode)
ENV_EXISTS=false
if [ -f "$ENV_FILE" ]; then
    ENV_EXISTS=true
    echo "Existing $ENV_FILE found. Will review and update configuration..."
else
    echo "No $ENV_FILE found. Creating new configuration..."
    touch "$ENV_FILE"
fi

echo ""
> "$TEMP_FILE"

while IFS= read -r line || [ -n "$line" ]; do
    # 1. Preserve comments and empty lines exactly as they are in .env.dist
    if [[ -z "$line" ]] || [[ "$line" == \#* ]]; then
        echo "$line" >> "$TEMP_FILE"
        continue
    fi

    # 2. Handle Variable Definitions
    if [[ "$line" == *=* ]]; then
        # Extract Key and Default Value
        key=$(echo "$line" | cut -d= -f1)
        dist_value=$(echo "$line" | cut -d= -f2-)
        key=$(echo "$key" | tr -d '[:space:]')

        # Check if key exists in current .env
        current_entry=$(grep "^${key}=" "$ENV_FILE" | head -n1)
        
        if [[ -n "$current_entry" ]]; then
            # Variable exists in .env
            current_value=$(echo "$current_entry" | cut -d= -f2-)
            
            if [ "$ENV_EXISTS" = true ]; then
                # Review mode: ask user to confirm each value
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                echo "  Variable: $key"
                echo "  Current:  $current_value"
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                
                if is_sensitive_var "$key"; then
                    # Sensitive variable: offer special options
                    if is_telegram_var "$key"; then
                        # Telegram variables: cannot be random
                        echo "  [k] Keep current value"
                        echo "  [m] Enter manually"
                        echo -n "  Choice [k]: " < /dev/tty
                        read choice < /dev/tty
                        choice=${choice:-k}
                        
                        case "$choice" in
                            m|M)
                                echo -n "  Enter new value: " < /dev/tty
                                read new_value < /dev/tty
                                if [[ -n "$new_value" ]]; then
                                    echo "$key=$new_value" >> "$TEMP_FILE"
                                    echo "  → Updated to new value"
                                else
                                    echo "$current_entry" >> "$TEMP_FILE"
                                    echo "  → Kept existing value"
                                fi
                                ;;
                            *)
                                echo "$current_entry" >> "$TEMP_FILE"
                                echo "  → Kept existing value"
                                ;;
                        esac
                    else
                        # Non-Telegram sensitive variable: can offer random generation
                        echo "  [k] Keep current value"
                        echo "  [g] Generate new random value"
                        echo "  [m] Enter manually"
                        echo -n "  Choice [k]: " < /dev/tty
                        read choice < /dev/tty
                        choice=${choice:-k}
                        
                        case "$choice" in
                            g|G)
                                secret_val=$(generate_secret)
                                echo "$key=$secret_val" >> "$TEMP_FILE"
                                echo "  → Generated: $secret_val"
                                ;;
                            m|M)
                                echo -n "  Enter new value: " < /dev/tty
                                read new_value < /dev/tty
                                if [[ -n "$new_value" ]]; then
                                    echo "$key=$new_value" >> "$TEMP_FILE"
                                    echo "  → Updated to new value"
                                else
                                    echo "$current_entry" >> "$TEMP_FILE"
                                    echo "  → Kept existing value"
                                fi
                                ;;
                            *)
                                echo "$current_entry" >> "$TEMP_FILE"
                                echo "  → Kept existing value"
                                ;;
                        esac
                    fi
                else
                    # Non-sensitive variable: simple confirm or change
                    echo -n "  Press Enter to keep, or type new value: " < /dev/tty
                    read user_input < /dev/tty
                    
                    if [[ -z "$user_input" ]]; then
                        echo "$current_entry" >> "$TEMP_FILE"
                        echo "  → Kept existing value"
                    else
                        echo "$key=$user_input" >> "$TEMP_FILE"
                        echo "  → Updated to: $user_input"
                    fi
                fi
                echo ""
            else
                # New file mode but variable somehow exists (shouldn't happen)
                echo "$current_entry" >> "$TEMP_FILE"
            fi
        else
            # Variable missing in .env, we need to populate it
            if is_sensitive_var "$key"; then
                if is_telegram_var "$key"; then
                    # Telegram variables: must be entered manually
                    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    echo "  NEW Variable: $key (Telegram - requires manual value)"
                    echo "  Default: $dist_value"
                    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    echo -n "  Enter value [$dist_value]: " < /dev/tty
                    read user_input < /dev/tty
                    
                    if [[ -z "$user_input" ]]; then
                        echo "$key=$dist_value" >> "$TEMP_FILE"
                    else
                        echo "$key=$user_input" >> "$TEMP_FILE"
                    fi
                    echo ""
                else
                    # Non-Telegram sensitive variable: generate random
                    echo "Generating secure value for $key..."
                    secret_val=$(generate_secret)
                    echo "$key=$secret_val" >> "$TEMP_FILE"
                fi
            else
                # Regular variable, prompt user
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                echo "  NEW Variable: $key"
                echo "  Default: $dist_value"
                echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                echo -n "  Enter value [$dist_value]: " < /dev/tty
                read user_input < /dev/tty
                
                if [[ -z "$user_input" ]]; then
                    echo "$key=$dist_value" >> "$TEMP_FILE"
                else
                    echo "$key=$user_input" >> "$TEMP_FILE"
                fi
                echo ""
            fi
        fi
    else
        # Lines that aren't A=B or comments (rare in .env but preserve them)
        echo "$line" >> "$TEMP_FILE"
    fi

done < "$DIST_FILE"

# Report removed variables (if any existed in old .env but not in .env.dist)
if [ "$ENV_EXISTS" = true ]; then
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Checking for obsolete variables..."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    removed_count=0
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ -n "$line" ]] && [[ "$line" != \#* ]] && [[ "$line" == *=* ]]; then
            old_key=$(echo "$line" | cut -d= -f1 | tr -d '[:space:]')
            # Check if this key exists in .env.dist
            if ! grep -q "^${old_key}=" "$DIST_FILE"; then
                old_value=$(echo "$line" | cut -d= -f2-)
                echo "  REMOVED: $old_key=$old_value"
                ((removed_count++))
            fi
        fi
    done < "$ENV_FILE"
    
    if [ "$removed_count" -eq 0 ]; then
        echo "  No obsolete variables found."
    else
        echo "  Total removed: $removed_count variable(s)"
    fi
fi

# Atomic replace
mv "$TEMP_FILE" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Ensure required directories exist with correct user ownership
if [ ! -d "mariadb_data" ]; then
    echo ""
    echo "Creating directory: mariadb_data"
    mkdir -p mariadb_data
fi

if [ ! -d "flex_queries" ]; then
    echo "Creating directory: flex_queries"
    mkdir -p flex_queries
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SUCCESS! $ENV_FILE has been updated."
echo "  Directories 'mariadb_data' and 'flex_queries' checked/created."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
