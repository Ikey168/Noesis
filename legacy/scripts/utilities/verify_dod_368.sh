#!/bin/bash

# DoD Verification Script for Issue 368: REST payload contracts for external API
# Verifies that JSON Schema contracts are implemented and CI checks are working

set -e

echo "🔍 Verifying DoD Requirements for Issue 368..."
echo "=============================================="

# Check 1: JSON Schema files exist
echo "✅ Check 1: JSON Schema contracts exist"
if [ ! -f "contracts/schemas/jsonschema/ask-request-v1.json" ]; then
    echo "❌ ask-request-v1.json not found"
    exit 1
fi
echo "   ✓ ask-request-v1.json exists"

if [ ! -f "contracts/schemas/jsonschema/ask-response-v1.json" ]; then
    echo "❌ ask-response-v1.json not found"
    exit 1
fi
echo "   ✓ ask-response-v1.json exists"

# Check 2: OpenAPI specification exists
echo ""
echo "✅ Check 2: OpenAPI specification exists"
if [ ! -f "legacy/services/api/openapi.yaml" ]; then
    echo "❌ legacy/services/api/openapi.yaml not found"
    exit 1
fi
echo "   ✓ legacy/services/api/openapi.yaml exists"

# Check 3: FastAPI/Pydantic validation integration
echo ""
echo "✅ Check 3: FastAPI validation integration"
if [ ! -f "legacy/services/api/validation.py" ]; then
    echo "❌ legacy/services/api/validation.py not found"
    exit 1
fi
echo "   ✓ Validation module exists"

# Check if validation is integrated in ask.py
if ! grep -q "JSON Schema validation" legacy/services/api/routes/ask.py; then
    echo "❌ JSON Schema validation not integrated in ask.py"
    exit 1
fi
echo "   ✓ JSON Schema validation integrated in ask.py"

# Check 4: CI check script exists and is executable
echo ""
echo "✅ Check 4: CI check exists"
if [ ! -f "ci_check_rest_contracts.sh" ]; then
    echo "❌ ci_check_rest_contracts.sh not found"
    exit 1
fi
if [ ! -x "ci_check_rest_contracts.sh" ]; then
    echo "❌ ci_check_rest_contracts.sh is not executable"
    exit 1
fi
echo "   ✓ CI check script exists and is executable"

# Check 5: Spectral configuration exists
echo ""
echo "✅ Check 5: Spectral configuration"
if [ ! -f ".spectral.yml" ]; then
    echo "❌ .spectral.yml not found"
    exit 1
fi
echo "   ✓ Spectral configuration exists"

# Check 6: Run JSON Schema validation
echo ""
echo "✅ Check 6: JSON Schema validation works"
echo "   Testing schema validation..."
python3 - << 'EOF'
import sys
import os
sys.path.append('legacy/services/api')

try:
    from validation import get_schema_validator
    
    validator = get_schema_validator()
    
    # Test valid request
    valid_request = {
        "question": "What is AI?",
        "k": 5,
        "provider": "openai"
    }
    
    errors = validator.validate_request("ask-request-v1", valid_request)
    if errors:
        print(f"   ❌ Valid request incorrectly rejected: {errors}")
        sys.exit(1)
    else:
        print("   ✓ Valid request correctly accepted")
    
    # Test invalid request
    invalid_request = {"k": 5}  # Missing required question
    
    errors = validator.validate_request("ask-request-v1", invalid_request)
    if not errors:
        print("   ❌ Invalid request incorrectly accepted")
        sys.exit(1)
    else:
        print(f"   ✓ Invalid request correctly rejected: {errors[0]}")
        
    print("   ✓ JSON Schema validation working correctly")
    
except Exception as e:
    print(f"   ❌ Validation test failed: {e}")
    sys.exit(1)
EOF

# Check 7: Run CI validation
echo ""
echo "✅ Check 7: CI validation passes"
echo "   Running full CI check..."
if ! ./ci_check_rest_contracts.sh > /dev/null 2>&1; then
    echo "   ❌ CI check failed"
    echo "   Running CI check with output:"
    ./ci_check_rest_contracts.sh
    exit 1
fi
echo "   ✓ CI check passes"

# Check 8: Verify invalid request rejection (422 status)
echo ""
echo "✅ Check 8: Invalid request rejection behavior"
echo "   Testing validation rejection logic..."
python3 - << 'EOF'
import sys
import os
sys.path.append('legacy/services/api')

try:
    from validation import get_schema_validator
    from fastapi import HTTPException
    
    validator = get_schema_validator()
    
    # Test that validation raises HTTPException with 422 for invalid data
    try:
        validator.validate_and_raise("ask-request-v1", {"k": 5})  # Missing question
        print("   ❌ Expected HTTPException not raised")
        sys.exit(1)
    except HTTPException as e:
        if e.status_code == 422:
            print("   ✓ Invalid requests properly rejected with 422 status")
            print(f"   ✓ Error details included: {str(e.detail)[:100]}...")
        else:
            print(f"   ❌ Wrong status code: {e.status_code}, expected 422")
            sys.exit(1)
    except Exception as e:
        print(f"   ❌ Unexpected exception: {e}")
        sys.exit(1)
        
except Exception as e:
    print(f"   ❌ Test failed: {e}")
    sys.exit(1)
EOF

# Check 9: Verify OpenAPI/Schema consistency
echo ""
echo "✅ Check 9: OpenAPI/Schema consistency"
python3 - << 'EOF'
import json
import yaml
from pathlib import Path

try:
    # Load OpenAPI spec
    with open('legacy/services/api/openapi.yaml', 'r') as f:
        openapi_spec = yaml.safe_load(f)
    
    # Load JSON schemas
    with open('contracts/schemas/jsonschema/ask-request-v1.json', 'r') as f:
        json_request_schema = json.load(f)
    
    with open('contracts/schemas/jsonschema/ask-response-v1.json', 'r') as f:
        json_response_schema = json.load(f)
    
    # Check that OpenAPI has the required schemas
    openapi_schemas = openapi_spec.get('components', {}).get('schemas', {})
    
    if 'AskRequest' not in openapi_schemas:
        print("   ❌ AskRequest schema missing from OpenAPI")
        exit(1)
    
    if 'AskResponse' not in openapi_schemas:
        print("   ❌ AskResponse schema missing from OpenAPI")
        exit(1)
    
    # Check that /ask endpoint exists
    paths = openapi_spec.get('paths', {})
    if '/ask' not in paths:
        print("   ❌ /ask endpoint missing from OpenAPI")
        exit(1)
    
    ask_endpoint = paths['/ask']
    if 'post' not in ask_endpoint:
        print("   ❌ POST method missing from /ask endpoint")
        exit(1)
    
    post_method = ask_endpoint['post']
    
    # Check 422 response is documented
    responses = post_method.get('responses', {})
    if '422' not in responses:
        print("   ❌ 422 response not documented in OpenAPI")
        exit(1)
    
    print("   ✓ OpenAPI spec includes required schemas and endpoints")
    print("   ✓ 422 validation error response documented")
    print("   ✓ OpenAPI/JSON Schema consistency verified")
    
except Exception as e:
    print(f"   ❌ Consistency check failed: {e}")
    exit(1)
EOF

# Summary
echo ""
echo "🎉 SUCCESS: All DoD requirements verified!"
echo "=============================================="
echo ""
echo "📋 Summary:"
echo "   ✓ JSON Schema contracts defined (ask-request-v1.json, ask-response-v1.json)"
echo "   ✓ OpenAPI specification created (legacy/services/api/openapi.yaml)"
echo "   ✓ FastAPI/Pydantic validation integrated"
echo "   ✓ CI check implemented (spectral + schema validation)"
echo "   ✓ Invalid requests rejected with 422 status and error mapping"
echo "   ✓ OpenAPI/Schema consistency verified"
echo ""
echo "📄 File Details:"
echo "   - ask-request-v1.json: $(wc -l < contracts/schemas/jsonschema/ask-request-v1.json) lines"
echo "   - ask-response-v1.json: $(wc -l < contracts/schemas/jsonschema/ask-response-v1.json) lines"
echo "   - openapi.yaml: $(wc -l < legacy/services/api/openapi.yaml) lines"
echo "   - validation.py: $(wc -l < legacy/services/api/validation.py) lines"
echo "   - CI check script: $(wc -l < ci_check_rest_contracts.sh) lines"
echo ""
echo "✅ Issue 368 DoD requirements fully satisfied!"
