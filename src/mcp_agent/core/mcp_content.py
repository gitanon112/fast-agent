"""
Helper functions for creating MCP content types with minimal code.

This module provides simple functions to create TextContent, ImageContent,
EmbeddedResource, and other MCP content types with minimal boilerplate.
"""

import base64
from pathlib import Path
from typing import Any, List, Literal, Optional, Union

from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)

from mcp_agent.mcp.mime_utils import (
    guess_mime_type,
    is_binary_content,
    is_image_mime_type,
)


def MCPText(
    text: str,
    role: Literal["user", "assistant"] = "user",
    annotations: Optional[dict] = None,
) -> dict:
    """
    Create a message with text content.

    Args:
        text: The text content
        role: Role of the message, defaults to "user"
        annotations: Optional annotations

    Returns:
        A dictionary with role and content that can be used in a prompt
    """
    return {
        "role": role,
        "content": TextContent(type="text", text=text, annotations=annotations),
    }


def MCPImage(
    path: Union[str, Path] = None,
    data: bytes = None,
    mime_type: Optional[str] = None,
    role: Literal["user", "assistant"] = "user",
    annotations: Optional[dict] = None,
) -> dict:
    """
    Create a message with image content.

    Args:
        path: Path to the image file
        data: Raw image data bytes (alternative to path)
        mime_type: Optional mime type, will be guessed from path if not provided
        role: Role of the message, defaults to "user"
        annotations: Optional annotations

    Returns:
        A dictionary with role and content that can be used in a prompt
    """
    if path is None and data is None:
        raise ValueError("Either path or data must be provided")

    if path is not None and data is not None:
        raise ValueError("Only one of path or data can be provided")

    if path is not None:
        path = Path(path)
        if not mime_type:
            mime_type = guess_mime_type(str(path))
        with open(path, "rb") as f:
            data = f.read()

    if not mime_type:
        mime_type = "image/png"  # Default

    b64_data = base64.b64encode(data).decode("ascii")

    return {
        "role": role,
        "content": ImageContent(type="image", data=b64_data, mimeType=mime_type, annotations=annotations),
    }


def MCPFile(
    path: Union[str, Path],
    mime_type: Optional[str] = None,
    role: Literal["user", "assistant"] = "user",
    annotations: Optional[dict] = None,
) -> dict:
    """
    Create a message with an embedded resource from a file.

    Args:
        path: Path to the resource file
        mime_type: Optional mime type, will be guessed from path if not provided
        role: Role of the message, defaults to "user"
        annotations: Optional annotations

    Returns:
        A dictionary with role and content that can be used in a prompt
    """
    path = Path(path)
    uri = f"file://{path.absolute()}"

    if not mime_type:
        mime_type = guess_mime_type(str(path))

    # Determine if this is text or binary content
    is_binary = is_binary_content(mime_type)

    if is_binary:
        # Read as binary
        binary_data = path.read_bytes()
        b64_data = base64.b64encode(binary_data).decode("ascii")

        resource = BlobResourceContents(uri=uri, blob=b64_data, mimeType=mime_type)
    else:
        # Read as text
        try:
            text_data = path.read_text(encoding="utf-8")
            resource = TextResourceContents(uri=uri, text=text_data, mimeType=mime_type)
        except UnicodeDecodeError:
            # Fallback to binary if text read fails
            binary_data = path.read_bytes()
            b64_data = base64.b64encode(binary_data).decode("ascii")
            resource = BlobResourceContents(uri=uri, blob=b64_data, mimeType=mime_type or "application/octet-stream")

    return {
        "role": role,
        "content": EmbeddedResource(type="resource", resource=resource, annotations=annotations),
    }


def MCPPrompt(*content_items, role: Literal["user", "assistant"] = "user") -> List[dict]:
    """
    Create one or more prompt messages with various content types.

    This function intelligently creates different content types:
    - Strings become TextContent
    - File paths with image mime types become ImageContent
    - File paths with text mime types or other mime types become EmbeddedResource
    - Dicts with role and content are passed through unchanged
    - Raw bytes become ImageContent

    Args:
        *content_items: Content items of various types
        role: Role for all items (user or assistant)

    Returns:
        List of messages that can be used in a prompt
    """
    result = []

    for item in content_items:
        if isinstance(item, dict) and "role" in item and "content" in item:
            # Already a fully formed message
            result.append(item)
        elif isinstance(item, str) and not Path(item).exists():
            # Simple text content (that's not a file path)
            result.append(MCPText(item, role=role))
        elif isinstance(item, Path) or isinstance(item, str):
            # File path - determine the content type based on mime type
            path_str = str(item)
            mime_type = guess_mime_type(path_str)

            if is_image_mime_type(mime_type):
                # Image files (except SVG which is handled as text)
                result.append(MCPImage(path=item, role=role))
            else:
                # All other file types (text documents, PDFs, SVGs, etc.)
                result.append(MCPFile(path=item, role=role))
        elif isinstance(item, bytes):
            # Raw binary data, assume image
            result.append(MCPImage(data=item, role=role))
        else:
            # Try to convert to string
            result.append(MCPText(str(item), role=role))

    return result


def User(*content_items) -> List[dict]:
    """Create user message(s) with various content types."""
    return MCPPrompt(*content_items, role="user")


def Assistant(*content_items) -> List[dict]:
    """Create assistant message(s) with various content types."""
    return MCPPrompt(*content_items, role="assistant")


def create_message(content: Any, role: Literal["user", "assistant"] = "user") -> dict:
    """
    Create a single prompt message from content of various types.

    Args:
        content: Content of various types (str, Path, bytes, etc.)
        role: Role of the message

    Returns:
        A dictionary with role and content that can be used in a prompt
    """
    messages = MCPPrompt(content, role=role)
    return messages[0] if messages else {}
