import json
import sys
import textwrap
from io import StringIO
from pathlib import Path
from typing import Optional, TextIO, Union

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

PathOrStr = Union[Path, str]


class YamlModel(BaseModel):
    """Pydantic model that can be exported to yaml."""

    def to_yaml(
        self,
        output_path: Union[PathOrStr, TextIO],
        with_comments: bool = True,
        by_alias: bool = True,
    ):
        """Save configuration as a yaml file.

        Used to record the default-filled version of a supplied yaml.

        Parameters
        ----------
        output_path : Pathlike
            Path to the yaml file to save.
        with_comments : bool, default = False.
            Whether to add comments containing the type/descriptions to all fields.
        by_alias : bool, default = False.
            Whether to use the alias names for the fields.
            Passed to pydantic's ``to_json`` method.
            https://docs.pydantic.dev/usage/exporting_models/#modeljson
        """
        yaml_obj = self._to_yaml_obj(by_alias=by_alias)

        if with_comments:
            _add_comments(yaml_obj, self.schema())

        y = YAML()
        if hasattr(output_path, "write"):
            y.dump(yaml_obj, output_path)
        else:
            with open(output_path, "w") as f:
                y.dump(yaml_obj, f)

    @classmethod
    def from_yaml(cls, yaml_path: PathOrStr):
        """Load a configuration from a yaml file.

        Parameters
        ----------
        yaml_path : Pathlike
            Path to the yaml file to load.

        Returns
        -------
        Config
            Workflow configuration
        """
        y = YAML(typ="safe")
        with open(yaml_path, "r") as f:
            data = y.load(f)

        return cls(**data)

    @classmethod
    def print_yaml_schema(cls, output_path: Union[PathOrStr, TextIO] = sys.stdout):
        """Print/save an empty configuration with defaults filled in.

        Ignores the required `cslc_file_list` input, so a user can
        inspect all fields.

        Parameters
        ----------
        output_path : Pathlike
            Path or stream to save to the yaml file to.
            By default, prints to stdout.
        """
        # The .construct is a pydantic method to disable validation
        # https://docs.pydantic.dev/usage/models/#creating-models-without-validation
        cls.construct().to_yaml(output_path, with_comments=True)

    def _to_yaml_obj(self, by_alias: bool = True) -> CommentedMap:
        # Make the YAML object to add comments to
        # We can't just do `dumps` for some reason, need a stream
        y = YAML()
        ss = StringIO()
        y.dump(json.loads(self.json(by_alias=by_alias)), ss)
        yaml_obj = y.load(ss.getvalue())
        return yaml_obj


def _add_comments(
    loaded_yaml: CommentedMap,
    schema: dict,
    indent: int = 0,
    definitions: Optional[dict] = None,
):
    """Add comments above each YAML field using the pydantic model schema."""
    # Definitions are in schemas that contain nested pydantic Models
    if definitions is None:
        definitions = schema.get("definitions")

    for key, val in schema["properties"].items():
        reference = ""
        # Get sub-schema if it exists
        if "$ref" in val.keys():
            # At top level, example is 'outputs': {'$ref': '#/definitions/Outputs'}
            reference = val["$ref"]
        elif "allOf" in val.keys():
            # within 'definitions', it looks like
            #  'allOf': [{'$ref': '#/definitions/HalfWindow'}]
            reference = val["allOf"][0]["$ref"]

        ref_key = reference.split("/")[-1]
        if ref_key:  # The current property is a reference to something else
            if "enum" in definitions[ref_key]:  # type: ignore
                # This is just an Enum, not a sub schema.
                # Overwrite the value with the referenced value
                val = definitions[ref_key]  # type: ignore
            else:
                # The reference is a sub schema, so we need to recurse
                sub_schema = definitions[ref_key]  # type: ignore
                # Get the sub-model
                sub_loaded_yaml = loaded_yaml[key]
                # recurse on the sub-model
                _add_comments(
                    sub_loaded_yaml,
                    sub_schema,
                    indent=indent + 2,
                    definitions=definitions,
                )
                continue

        # add each description along with the type information
        desc = "\n".join(
            textwrap.wrap(f"{val['description']}.", width=90, subsequent_indent="  ")
        )
        type_str = f"\n  Type: {val['type']}."
        choices = f"\n  Options: {val['enum']}." if "enum" in val.keys() else ""

        # Combine the description/type/choices as the YAML comment
        comment = f"{desc}{type_str}{choices}"
        comment = comment.replace("..", ".")  # Remove double periods

        # Prepend the required label for fields that are required
        is_required = key in schema.get("required", [])
        if is_required:
            comment = "REQUIRED: " + comment

        # This method comes from here
        # https://yaml.readthedocs.io/en/latest/detail.html#round-trip-including-comments
        loaded_yaml.yaml_set_comment_before_after_key(key, comment, indent=indent)
