# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Utility class for converting between Avro and Iceberg schemas

"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from iceberg.schema import Schema
from iceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    FixedType,
    FloatType,
    IcebergType,
    IntegerType,
    ListType,
    LongType,
    MapType,
    NestedField,
    PrimitiveType,
    StringType,
    StructType,
    TimestampType,
    TimeType,
    UUIDType,
)

logger = logging.getLogger(__name__)

PRIMITIVE_FIELD_TYPE_MAPPING: Dict[str, PrimitiveType] = {
    "boolean": BooleanType(),
    "bytes": BinaryType(),
    "double": DoubleType(),
    "float": FloatType(),
    "int": IntegerType(),
    "long": LongType(),
    "string": StringType(),
    "enum": StringType(),
}

LOGICAL_FIELD_TYPE_MAPPING: Dict[Tuple[str, str], PrimitiveType] = {
    ("date", "int"): DateType(),
    ("time-millis", "int"): TimeType(),
    ("timestamp-millis", "long"): TimestampType(),
    ("time-micros", "int"): TimeType(),
    ("timestamp-micros", "long"): TimestampType(),
    ("uuid", "string"): UUIDType(),
}


class AvroSchemaConversion:
    def avro_to_iceberg(self, avro_schema: Dict[str, Any]) -> Schema:
        """Converts an Apache Avro into an Apache Iceberg schema equivalent

        This expects to have field id's to be encoded in the Avro schema::

            {
                "type": "record",
                "name": "manifest_file",
                "fields": [
                    {"name": "manifest_path", "type": "string", "doc": "Location URI with FS scheme", "field-id": 500},
                    {"name": "manifest_length", "type": "long", "doc": "Total file size in bytes", "field-id": 501}
                ]
            }

        Example:
            This converts an Avro schema into an Iceberg schema:

            >>> avro_schema = AvroSchemaConversion().avro_to_iceberg({
            ...     "type": "record",
            ...     "name": "manifest_file",
            ...     "fields": [
            ...         {"name": "manifest_path", "type": "string", "doc": "Location URI with FS scheme", "field-id": 500},
            ...         {"name": "manifest_length", "type": "long", "doc": "Total file size in bytes", "field-id": 501}
            ...     ]
            ... })
            >>> iceberg_schema = Schema(
            ...     NestedField(
            ...         field_id=500, name="manifest_path", field_type=StringType(), is_optional=False, doc="Location URI with FS scheme"
            ...     ),
            ...     NestedField(
            ...         field_id=501, name="manifest_length", field_type=LongType(), is_optional=False, doc="Total file size in bytes"
            ...     ),
            ...     schema_id=1
            ... )
            >>> avro_schema == iceberg_schema
            True

        Args:
            avro_schema (Dict[str, Any]): The JSON decoded Avro schema

        Returns:
            Equivalent Iceberg schema
        """
        return Schema(*[self._convert_field(field) for field in avro_schema["fields"]], schema_id=1)

    def _resolve_union(self, type_union: Dict | List | str) -> Tuple[str | Dict[str, Any], bool]:
        """
        Converts Unions into their type and resolves if the field is optional

        Examples:
            >>> AvroSchemaConversion()._resolve_union('str')
            ('str', False)
            >>> AvroSchemaConversion()._resolve_union(['null', 'str'])
            ('str', True)
            >>> AvroSchemaConversion()._resolve_union([{'type': 'str'}])
            ({'type': 'str'}, False)
            >>> AvroSchemaConversion()._resolve_union(['null', {'type': 'str'}])
            ({'type': 'str'}, True)

        Args:
            type_union: The field, can be a string 'str', list ['null', 'str'], or dict {"type": 'str'}

        Returns:
            A tuple containing the type and nullability

        Raises:
            TypeError: In the case non-optional union types are encountered
        """
        avro_types: Dict | List
        if isinstance(type_union, str):
            # It is a primitive and required
            return type_union, False
        elif isinstance(type_union, dict):
            # It is a context and required
            return type_union, False
        else:
            avro_types = type_union

        is_optional = "null" in avro_types

        if len(avro_types) > 2:
            raise TypeError("Non-optional types aren't part of the Iceberg specification")

        # Filter the null value and return the type
        return list(filter(lambda t: t != "null", avro_types))[0], is_optional

    def _convert_schema(self, avro_type: str | Dict[str, Any]) -> IcebergType:
        """
        Resolves the Avro type

        Args:
            avro_type: The Avro type, can be simple or complex

        Returns:
            The equivalent IcebergType

        Raises:
            ValueError: When there are unknown types
        """
        if isinstance(avro_type, str):
            return PRIMITIVE_FIELD_TYPE_MAPPING[avro_type]
        elif isinstance(avro_type, dict):
            if "logicalType" in avro_type:
                return self._convert_logical_type(avro_type)
            else:
                # Resolve potential nested types
                while "type" in avro_type and isinstance(avro_type["type"], dict):
                    avro_type = avro_type["type"]
                type_identifier = avro_type["type"]
                if type_identifier == "record":
                    return self._convert_record_type(avro_type)
                elif type_identifier == "array":
                    return self._convert_array_type(avro_type)
                elif type_identifier == "map":
                    return self._convert_map_type(avro_type)
                elif type_identifier == "fixed":
                    return self._convert_fixed_type(avro_type)
                elif isinstance(type_identifier, str):
                    return PRIMITIVE_FIELD_TYPE_MAPPING[type_identifier]
                else:
                    raise ValueError(f"Unknown type: {avro_type}")
        else:
            raise ValueError(f"Unknown type: {avro_type}")

    def _convert_field(self, field: Dict[str, Any]) -> NestedField:
        """
        Converts an Avro field into an Iceberg equivalent field
        Args:
            field: The Avro field

        Returns:
            The Iceberg equivalent field
        """
        if "field-id" not in field:
            raise ValueError(f"Cannot convert field, missing field-id: {field}")

        plain_type, is_optional = self._resolve_union(field["type"])

        return NestedField(
            field_id=field["field-id"],
            name=field["name"],
            field_type=self._convert_schema(plain_type),
            is_optional=is_optional,
            doc=field.get("doc"),
        )

    def _convert_record_type(self, record_type: Dict[str, Any]) -> StructType:
        """
        Converts the fields from a record into an Iceberg struct

        Examples:
            >>> from iceberg.utils.schema_conversion import AvroSchemaConversion
            >>> record_type = {
            ...     "type": "record",
            ...     "name": "r508",
            ...     "fields": [{
            ...         "name": "contains_null",
            ...         "type": "boolean",
            ...         "doc": "True if any file has a null partition value",
            ...         "field-id": 509,
            ...      }, {
            ...          "name": "contains_nan",
            ...          "type": ["null", "boolean"],
            ...          "doc": "True if any file has a nan partition value",
            ...          "default": None,
            ...          "field-id": 518,
            ...      }],
            ... }
            >>> actual = AvroSchemaConversion()._convert_record_type(record_type)
            >>> expected = StructType(
            ...     fields=(
            ...         NestedField(
            ...             field_id=509,
            ...             name="contains_null",
            ...             field_type=BooleanType(),
            ...             is_optional=False,
            ...             doc="True if any file has a null partition value",
            ...         ),
            ...         NestedField(
            ...             field_id=518,
            ...             name="contains_nan",
            ...             field_type=BooleanType(),
            ...             is_optional=True,
            ...             doc="True if any file has a nan partition value",
            ...         ),
            ...     )
            ... )
            >>> expected == actual
            True

        Args:
            record_type: The record type itself

        Returns:
        """
        if record_type["type"] != "record":
            raise ValueError(f"Expected type, got: {record_type}")

        return StructType(*[self._convert_field(field) for field in record_type["fields"]])

    def _convert_array_type(self, array_type: Dict[str, Any]) -> ListType:
        if "element-id" not in array_type:
            raise ValueError(f"Cannot convert array-type, missing element-id: {array_type}")

        plain_type, element_is_optional = self._resolve_union(array_type["items"])

        return ListType(
            element_id=array_type["element-id"],
            element_type=self._convert_schema(plain_type),
            element_is_optional=element_is_optional,
        )

    def _convert_map_type(self, map_type: Dict[str, Any]) -> MapType:
        """
        Args:
            map_type: The dict that describes the Avro map type

        Examples:
            >>> from iceberg.utils.schema_conversion import AvroSchemaConversion
            >>> avro_field = {
            ...     "type": "map",
            ...     "values": ["long", "null"],
            ...     "key-id": 101,
            ...     "value-id": 102,
            ... }
            >>> actual = AvroSchemaConversion()._convert_map_type(avro_field)
            >>> expected = MapType(
            ...     key_id=101,
            ...     key_type=StringType(),
            ...     value_id=102,
            ...     value_type=LongType(),
            ...     value_is_optional=True
            ... )
            >>> actual == expected
            True

        Returns: A MapType
        """
        value_type, value_is_optional = self._resolve_union(map_type["values"])
        return MapType(
            key_id=map_type["key-id"],
            # Avro only supports string keys
            key_type=StringType(),
            value_id=map_type["value-id"],
            value_type=self._convert_schema(value_type),
            value_is_optional=value_is_optional,
        )

    def _convert_logical_type(self, avro_logical_type: Dict[str, Any]) -> IcebergType:
        """
        Convert a schema with a logical type annotation. For the decimal and map
        we need to fetch more keys from the dict, and for the simple ones we can just
        look it up in the mapping.

        Examples:
            >>> from iceberg.utils.schema_conversion import AvroSchemaConversion
            >>> avro_logical_type = {
            ...     "type": "int",
            ...     "logicalType": "date"
            ... }
            >>> actual = AvroSchemaConversion()._convert_logical_type(avro_logical_type)
            >>> actual == DateType()
            True

        Args:
            avro_logical_type: The logical type

        Returns:
            The converted logical type

        Raises:
            ValueError: When the logical type is unknown
        """
        logical_type = avro_logical_type["logicalType"]
        physical_type = avro_logical_type["type"]
        if logical_type == "decimal":
            return self._convert_logical_decimal_type(avro_logical_type)
        elif logical_type == "map":
            return self._convert_logical_map_type(avro_logical_type)
        elif (logical_type, physical_type) in LOGICAL_FIELD_TYPE_MAPPING:
            return LOGICAL_FIELD_TYPE_MAPPING[(logical_type, physical_type)]
        else:
            raise ValueError(f"Unknown logical/physical type combination: {avro_logical_type}")

    def _convert_logical_decimal_type(self, avro_type: Dict[str, Any]) -> DecimalType:
        """
        Args:
            avro_type: The Avro type

        Examples:
            >>> from iceberg.utils.schema_conversion import AvroSchemaConversion
            >>> avro_decimal_type = {
            ...     "type": "bytes",
            ...     "logicalType": "decimal",
            ...     "precision": 19,
            ...     "scale": 25
            ... }
            >>> actual = AvroSchemaConversion()._convert_logical_decimal_type(avro_decimal_type)
            >>> expected = DecimalType(
            ...     precision=19,
            ...     scale=25
            ... )
            >>> actual == expected
            True

        Returns:
            A Iceberg DecimalType
        """
        return DecimalType(precision=avro_type["precision"], scale=avro_type["scale"])

    def _convert_logical_map_type(self, avro_type: Dict[str, Any]) -> MapType:
        """
        In the case where a map hasn't a key as a type you can use a logical map to
        still encode this in Avro

        Args:
            avro_type: The Avro Type

        Examples:
            >>> from iceberg.utils.schema_conversion import AvroSchemaConversion
            >>> avro_type = {
            ...     "type": "array",
            ...     "logicalType": "map",
            ...     "items": {
            ...         "type": "record",
            ...         "name": "k101_v102",
            ...         "fields": [
            ...             {"name": "key", "type": "int", "field-id": 101},
            ...             {"name": "value", "type": "string", "field-id": 102},
            ...         ],
            ...     },
            ... }
            >>> actual = AvroSchemaConversion()._convert_logical_map_type(avro_type)
            >>> expected = MapType(
            ...         key_id=101,
            ...         key_type=IntegerType(),
            ...         value_id=102,
            ...         value_type=StringType(),
            ...         value_is_optional=False
            ... )
            >>> actual == expected
            True

        .. _Apache Iceberg specification:
            https://iceberg.apache.org/spec/#appendix-a-format-specific-requirements

        Returns:
            The logical map
        """
        fields = avro_type["items"]["fields"]
        if len(fields) != 2:
            raise ValueError(f'Invalid key-value pair schema: {avro_type["items"]}')
        key = self._convert_field(list(filter(lambda f: f["name"] == "key", fields))[0])
        value = self._convert_field(list(filter(lambda f: f["name"] == "value", fields))[0])
        return MapType(
            key_id=key.field_id,
            key_type=key.field_type,
            value_id=value.field_id,
            value_type=value.field_type,
            value_is_optional=value.is_optional,
        )

    def _convert_fixed_type(self, avro_type: Dict[str, Any]) -> FixedType:
        """
        https://avro.apache.org/docs/current/spec.html#Fixed

        Args:
            avro_type: The Avro Type

        Examples:
            >>> from iceberg.utils.schema_conversion import AvroSchemaConversion
            >>> avro_fixed_type = {
            ...     "name": "md5",
            ...     "type": "fixed",
            ...     "size": 16
            ... }
            >>> FixedType(length=16) == AvroSchemaConversion()._convert_fixed_type(avro_fixed_type)
            True

        Returns:
            An Iceberg equivalent fixed type
        """
        return FixedType(length=avro_type["size"])
