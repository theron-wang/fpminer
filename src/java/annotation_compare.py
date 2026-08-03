import tree_sitter_java
from tree_sitter import Language, Parser

JAVA = Language(tree_sitter_java.language())
JAVA_PARSER = Parser(JAVA)

ANNOTATION_NODE_TYPES = {"marker_annotation", "annotation"}
COMMENT_NODE_TYPES = {"line_comment", "block_comment"}


def _collect_tokens(node):
    """
    Recursively collect (type, text) tuples for every leaf token in the tree,
    skipping any subtree rooted at an excluded node type (i.e. annotations or comments).
    """
    if node.type in ANNOTATION_NODE_TYPES or node.type in COMMENT_NODE_TYPES:
        return []

    if node.child_count == 0:
        return [(node.type, node.text)]

    tokens = []
    for child in node.children:
        tokens.extend(_collect_tokens(child))
    return tokens


def are_changes_annotation_or_comment_only(original: str, modified: str) -> bool:
    """
    Returns True if the original and modified Java source code differ only in
    annotations and comments, and False otherwise.
    :param original: The original code
    :param modified: The modified code
    :return: True if the changes are annotation or comment-only, False otherwise
    """
    original_tree = JAVA_PARSER.parse(original.encode("utf-8"))
    modified_tree = JAVA_PARSER.parse(modified.encode("utf-8"))

    original_tokens = _collect_tokens(original_tree.root_node)
    modified_tokens = _collect_tokens(modified_tree.root_node)

    return original_tokens == modified_tokens
