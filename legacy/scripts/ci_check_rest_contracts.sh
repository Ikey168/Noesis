#!/bin/bash

# CI Check for REST Payload Contracts - Issue #368
# Validates OpenAPI spec against JSON Schema contracts

set -e

echo "🔍 Validating REST API Contracts..."
echo "==================================="

# Check if required tools are available
echo "✅ Checking required tools..."

# Check for Python and required packages
if ! python3 -c "import jsonschema, yaml, json" 2>/dev/null; then
    echo "Installing required Python packages..."
    pip install jsonschema pyyaml
fi

# Check for spectral (OpenAPI linter)
if ! command -v spectral &> /dev/null; then
    echo "Installing Spectral OpenAPI linter..."
    npm install -g @stoplight/spectral-cli
fi

echo "   ✓ Required tools available"

# Check 1: JSON Schema files are valid
echo ""
echo "✅ Check 1: Validating JSON Schema files"
for schema_file in contracts/schemas/jsonschema/*.json; do
    if [ -f "$schema_file" ]; then
        echo "   Validating $(basename "$schema_file")..."
        if ! python3 -c "
import json
import jsonschema
with open('$schema_file', 'r') as f:
    schema = json.load(f)
    jsonschema.Draft7Validator.check_schema(schema)
print('   ✓ Valid JSON Schema')
        "; then
            echo "   ❌ Invalid JSON Schema: $schema_file"
            exit 1
        fi
    fi
done

# Check 2: OpenAPI spec is valid
echo ""
echo "✅ Check 2: Validating OpenAPI specification"
if [ -f "legacy/services/api/openapi.yaml" ]; then
    echo "   Linting OpenAPI spec with Spectral..."
    if ! spectral lint legacy/services/api/openapi.yaml; then
        echo "   ❌ OpenAPI spec has validation errors"
        exit 1
    fi
    echo "   ✓ OpenAPI spec is valid"
else
    echo "   ❌ OpenAPI spec not found: legacy/services/api/openapi.yaml"
    exit 1
fi

# Check 3: OpenAPI schemas match JSON Schema contracts
echo ""
echo "✅ Check 3: Checking OpenAPI/JSON Schema consistency"
python3 - << 'EOF'
import json
import yaml
from pathlib import Path

# Load OpenAPI spec
with open('legacy/services/api/openapi.yaml', 'r') as f:
    openapi_spec = yaml.safe_load(f)

# Load JSON schemas
schemas_dir = Path('contracts/schemas/jsonschema')
json_schemas = {}
for schema_file in schemas_dir.glob('*.json'):
    with open(schema_file, 'r') as f:
        json_schemas[schema_file.stem] = json.load(f)

def check_schema_consistency(openapi_schema, json_schema, path=""):
    """Check if OpenAPI schema is consistent with JSON Schema."""
    errors = []
    
    # Check required fields
    openapi_required = set(openapi_schema.get('required', []))
    json_required = set(json_schema.get('required', []))
    
    if openapi_required != json_required:
        errors.append(f"{path}: Required fields mismatch")
        errors.append(f"  OpenAPI: {openapi_required}")
        errors.append(f"  JSON Schema: {json_required}")
    
    # Check properties
    openapi_props = openapi_schema.get('properties', {})
    json_props = json_schema.get('properties', {})
    
    for prop_name in openapi_props:
        if prop_name not in json_props:
            errors.append(f"{path}.{prop_name}: Property missing in JSON Schema")
    
    for prop_name in json_props:
        if prop_name not in openapi_props:
            errors.append(f"{path}.{prop_name}: Property missing in OpenAPI")
    
    return errors

# Check AskRequest consistency
print("   Checking AskRequest schema consistency...")
if 'ask-request-v1' in json_schemas:
    openapi_ask_request = openapi_spec['components']['schemas']['AskRequest']
    json_ask_request = json_schemas['ask-request-v1']
    
    errors = check_schema_consistency(openapi_ask_request, json_ask_request, "AskRequest")
    if errors:
        print("   ❌ AskRequest schema inconsistencies found:")
        for error in errors:
            print(f"      {error}")
        exit(1)
    else:
        print("   ✓ AskRequest schemas are consistent")

# Check AskResponse consistency  
print("   Checking AskResponse schema consistency...")
if 'ask-response-v1' in json_schemas:
    openapi_ask_response = openapi_spec['components']['schemas']['AskResponse']
    json_ask_response = json_schemas['ask-response-v1']
    
    errors = check_schema_consistency(openapi_ask_response, json_ask_response, "AskResponse")
    if errors:
        print("   ❌ AskResponse schema inconsistencies found:")
        for error in errors:
            print(f"      {error}")
        exit(1)
    else:
        print("   ✓ AskResponse schemas are consistent")

print("   ✓ All schemas are consistent")
EOF

# Check 4: Validate example requests against schemas
echo ""
echo "✅ Check 4: Validating example requests"
python3 - << 'EOF'
import json
import jsonschema
from pathlib import Path

# Load schemas
schemas_dir = Path('contracts/schemas/jsonschema')
with open(schemas_dir / 'ask-request-v1.json', 'r') as f:
    request_schema = json.load(f)

with open(schemas_dir / 'ask-response-v1.json', 'r') as f:
    response_schema = json.load(f)

# Create validators
request_validator = jsonschema.Draft7Validator(request_schema)
response_validator = jsonschema.Draft7Validator(response_schema)

# Test examples from the schemas
print("   Validating request examples...")
for i, example in enumerate(request_schema.get('examples', [])):
    try:
        request_validator.validate(example)
        print(f"   ✓ Request example {i+1} is valid")
    except jsonschema.ValidationError as e:
        print(f"   ❌ Request example {i+1} validation failed: {e.message}")
        exit(1)

print("   Validating response examples...")
for i, example in enumerate(response_schema.get('examples', [])):
    try:
        response_validator.validate(example)
        print(f"   ✓ Response example {i+1} is valid")
    except jsonschema.ValidationError as e:
        print(f"   ❌ Response example {i+1} validation failed: {e.message}")
        exit(1)

print("   ✓ All examples are valid")
EOF

# Check 5: Test validation with invalid payloads
echo ""
echo "✅ Check 5: Testing validation rejection of invalid payloads"
python3 - << 'EOF'
import json
import jsonschema
from pathlib import Path

# Load request schema
schemas_dir = Path('contracts/schemas/jsonschema')
with open(schemas_dir / 'ask-request-v1.json', 'r') as f:
    request_schema = json.load(f)

validator = jsonschema.Draft7Validator(request_schema)

# Test invalid payloads that should be rejected
invalid_payloads = [
    # Missing required question field
    {"k": 5},
    
    # Question too short
    {"question": "Hi"},
    
    # Question too long
    {"question": "x" * 501},
    
    # Invalid k value
    {"question": "What is AI?", "k": 0},
    {"question": "What is AI?", "k": 25},
    
    # Invalid provider
    {"question": "What is AI?", "provider": "invalid_provider"},
    
    # Invalid filter properties
    {"question": "What is AI?", "filters": {"invalid_filter": "value"}},
    
    # Invalid language in filters
    {"question": "What is AI?", "filters": {"language": "invalid_lang"}},
]

print("   Testing invalid payload rejection...")
for i, payload in enumerate(invalid_payloads):
    errors = list(validator.iter_errors(payload))
    if not errors:
        print(f"   ❌ Invalid payload {i+1} was incorrectly accepted: {payload}")
        exit(1)

print(f"   ✓ All {len(invalid_payloads)} invalid payloads correctly rejected")
EOF

echo ""
echo "🎉 SUCCESS: All REST API contract validations passed!"
echo "================================================="
echo ""
echo "📋 Summary:"
echo "   ✓ JSON Schema files are valid"
echo "   ✓ OpenAPI specification is valid"
echo "   ✓ OpenAPI/JSON Schema consistency verified"
echo "   ✓ Example requests/responses validate correctly"
echo "   ✓ Invalid requests are properly rejected"
echo ""
echo "✅ Issue 368 validation requirements satisfied!"
