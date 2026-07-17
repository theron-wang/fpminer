from pathlib import Path
from typing import Tuple

import tree_sitter_java
from tree_sitter import Language, Parser, Point, Tree, Node
from utils import CheckerError

TYPE_DECLS = ["class_declaration",
              "interface_declaration",
              "enum_declaration",
              "record_declaration",
              "annotation_type_declaration"]


def _get_target(tree: Tree, source: bytes, error: CheckerError) -> Tuple[Node | None, bool]:
    """
    Gets the target method/field that the error occurred at.
    :param tree: The AST
    :param source: The original source code
    :param error: The error
    :return: The node (or None if not found), and True if the NullAway modularity model should be used
    """
    # use the end; if we use the beginning and the error is on a constructor/method
    # declaration line, then choosing the beginning will result in a search of the
    # class body node (which will cause an error)
    row_length = len(source.splitlines()[error.line_number - 1])

    node = tree.root_node.descendant_for_point_range(Point(row=error.line_number - 1, column=row_length - 1),
                                                     Point(row=error.line_number - 1, column=row_length - 1))

    while node is not None:
        if node.type == "method_declaration" or node.type == "constructor_declaration" or node.type == "field_declaration":
            if not _is_node_in_anonymous_class(node):
                return node, node.start_point.row == error.line_number - 1
        node = node.parent

    return node, False


def _get_package_name(tree: Tree) -> str | None:
    root = tree.root_node

    for child in root.children:
        if child.type == "package_declaration":
            for sub in child.children:
                if sub.type in ("scoped_identifier", "identifier"):
                    return _convert_node_to_string(sub)
    return None


def _get_encapsulating_type(node: Node) -> Node | None:
    while node is not None and node.type not in TYPE_DECLS:
        node = node.parent
    return node


def _get_simple_class_signature(node: Node) -> str:
    if node.type not in TYPE_DECLS:
        raise ValueError(f"Node is not a type declaration: {node.type}")

    name_node = node.child_by_field_name("name")
    assert name_node is not None

    name = _convert_node_to_string(name_node)
    # suppresses the linter in IntelliJ
    parent = node.parent

    if not parent:
        return name

    parent_class = _get_encapsulating_type(parent)

    if not parent_class:
        return name

    return f"{_get_simple_class_signature(parent_class)}.{name}"


def _is_node_in_anonymous_class(node: Node) -> bool:
    # While this will technically capture arguments in constructor calls,
    # we'll only be using this method on method declaration nodes.
    while node is not None and node.type != "object_creation_expression":
        node = node.parent
    return node is not None


def _convert_node_to_string(node: Node) -> str:
    return node.text.decode("utf-8")


def get_target_signature_and_modularity_model(root_dir: Path, error: CheckerError) -> Tuple[list[str], bool]:
    java = Language(tree_sitter_java.language())
    parser = Parser(java)

    with open(root_dir / error.file_path, 'rb') as f:
        source = f.read()

    tree = parser.parse(source)

    target, nullaway = _get_target(tree, source, error)

    assert target is not None

    package = _get_package_name(tree)
    encapsulating_type = _get_encapsulating_type(target)

    assert encapsulating_type is not None

    encapsulating_type_name = _get_simple_class_signature(encapsulating_type)

    if target.type == "field_declaration":
        fields = []
        for child in target.children:
            if child.type != "variable_declarator":
                continue
            field_name_node = child.child_by_field_name("name")
            assert field_name_node is not None

            fields.append(_convert_node_to_string(field_name_node))

        return [f"{f"{package}." if package else ""}{encapsulating_type_name}#{field_name}" for field_name in
                fields], False

    method_name_node = target.child_by_field_name("name")

    assert method_name_node is not None

    method_name = _convert_node_to_string(method_name_node)

    param_nodes = target.child_by_field_name("parameters")
    assert param_nodes is not None
    params: list[str] = []

    for param in param_nodes.children:
        if param.type == "formal_parameter":
            param_type_node = param.child_by_field_name("type")
            assert param_type_node is not None
            params.append(_convert_node_to_string(param_type_node))
        elif param.type == "spread_parameter":
            param_type_node = next(node for node in param.children if node.type == "type_identifier")
            assert param_type_node is not None
            params.append(_convert_node_to_string(param_type_node) + "...")

    return [f"{f"{package}." if package else ""}{encapsulating_type_name}#{method_name}({",".join(params)})"], nullaway
