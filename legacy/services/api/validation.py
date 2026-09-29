"""
JSON Schema validation utilities for API contracts.

This module provides utilities for validating requests and responses
against JSON Schema contracts defined in the contracts directory.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema
from fastapi import HTTPException
from jsonschema import Draft7Validator
from pydantic import BaseModel


class SchemaValidator:
    """JSON Schema validator for API contracts."""
    
    def __init__(self, schema_dir: str = "contracts/schemas/jsonschema"):
        """Initialize validator with schema directory."""
        self.schema_dir = Path(schema_dir)
        self._schemas: Dict[str, Dict[str, Any]] = {}
        self._validators: Dict[str, Draft7Validator] = {}
        self._load_schemas()
    
    def _load_schemas(self) -> None:
        """Load all JSON schemas from the schema directory."""
        if not self.schema_dir.exists():
            return
            
        for schema_file in self.schema_dir.glob("*.json"):
            schema_name = schema_file.stem
            try:
                with open(schema_file, 'r') as f:
                    schema = json.load(f)
                    self._schemas[schema_name] = schema
                    self._validators[schema_name] = Draft7Validator(schema)
                    print(f"Loaded schema: {schema_name}")
            except Exception as e:
                print(f"Error loading schema {schema_file}: {e}")
    
    def validate_request(self, schema_name: str, data: Dict[str, Any]) -> List[str]:
        """
        Validate request data against schema.
        
        Args:
            schema_name: Name of the schema (without .json extension)
            data: Request data to validate
            
        Returns:
            List of validation error messages (empty if valid)
        """
        if schema_name not in self._validators:
            return [f"Schema '{schema_name}' not found"]
        
        validator = self._validators[schema_name]
        errors = []
        
        for error in validator.iter_errors(data):
            # Format error message for better readability
            path = " -> ".join(str(p) for p in error.path) if error.path else "root"
            errors.append(f"{path}: {error.message}")
        
        return errors
    
    def validate_and_raise(self, schema_name: str, data: Dict[str, Any]) -> None:
        """
        Validate data and raise HTTPException if invalid.
        
        Args:
            schema_name: Name of the schema
            data: Data to validate
            
        Raises:
            HTTPException: 422 error with validation details
        """
        errors = self.validate_request(schema_name, data)
        if errors:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Validation failed",
                    "errors": errors,
                    "schema": schema_name
                }
            )
    
    def get_schema(self, schema_name: str) -> Optional[Dict[str, Any]]:
        """Get schema by name."""
        return self._schemas.get(schema_name)
    
    def list_schemas(self) -> List[str]:
        """List all available schema names."""
        return list(self._schemas.keys())


# Global validator instance
_validator: Optional[SchemaValidator] = None


def get_schema_validator() -> SchemaValidator:
    """Get or create the global schema validator instance."""
    global _validator
    if _validator is None:
        # Try to find schema directory relative to project root
        current_dir = Path(__file__).parent
        
        # Try multiple possible paths
        possible_paths = [
            current_dir.parent.parent / "contracts" / "schemas" / "jsonschema",  # From legacy/services/api/
            current_dir.parent.parent.parent / "contracts" / "schemas" / "jsonschema",  # From project root
            Path("contracts/schemas/jsonschema"),  # Relative to current working directory
        ]
        
        schema_dir = None
        for path in possible_paths:
            if path.exists():
                schema_dir = path
                break
        
        if schema_dir is None:
            print("Warning: Could not find JSON Schema directory")
            schema_dir = "contracts/schemas/jsonschema"  # Fallback
            
        _validator = SchemaValidator(str(schema_dir))
    return _validator


def validate_ask_request(data: Dict[str, Any]) -> None:
    """Validate ask request data against JSON Schema."""
    validator = get_schema_validator()
    validator.validate_and_raise("ask-request-v1", data)


def validate_ask_response(data: Dict[str, Any]) -> None:
    """Validate ask response data against JSON Schema."""
    validator = get_schema_validator()
    validator.validate_and_raise("ask-response-v1", data)


class JsonSchemaValidatedModel(BaseModel):
    """Base model that validates against JSON Schema in addition to Pydantic validation."""
    
    def __init__(self, **data):
        # First, validate with JSON Schema
        self._validate_json_schema(data)
        # Then use normal Pydantic validation
        super().__init__(**data)
    
    def _validate_json_schema(self, data: Dict[str, Any]) -> None:
        """Override in subclasses to specify schema name."""
        pass
    
    class Config:
        # Generate JSON schema that matches our contract schemas
        schema_extra = {
            "additionalProperties": False
        }


class ValidatedAskRequest(JsonSchemaValidatedModel):
    """Ask request with JSON Schema validation."""
    
    question: str
    k: Optional[int] = 5
    filters: Optional[Dict[str, Any]] = None
    rerank_on: Optional[bool] = True
    fusion: Optional[bool] = True
    provider: Optional[str] = "openai"
    
    def _validate_json_schema(self, data: Dict[str, Any]) -> None:
        """Validate against ask-request-v1 schema."""
        validate_ask_request(data)


class ValidatedAskResponse(JsonSchemaValidatedModel):
    """Ask response with JSON Schema validation."""
    
    question: str
    answer: str
    citations: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    request_id: str
    tracked_in_mlflow: bool
    
    def _validate_json_schema(self, data: Dict[str, Any]) -> None:
        """Validate against ask-response-v1 schema."""
        validate_ask_response(data)
