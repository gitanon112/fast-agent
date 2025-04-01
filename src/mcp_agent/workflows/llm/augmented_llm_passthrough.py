import json  # Import at the module level
from typing import Any, List, Optional, Type, Union

from mcp.types import PromptMessage
from pydantic_core import from_json

from mcp_agent.core.prompt import Prompt
from mcp_agent.logging.logger import get_logger
from mcp_agent.mcp.prompt_message_multipart import PromptMessageMultipart
from mcp_agent.workflows.llm.augmented_llm import (
    AugmentedLLM,
    MessageParamT,
    MessageT,
    ModelT,
    RequestParams,
)

CALL_TOOL_INDICATOR = "***CALL_TOOL"
FIXED_RESPONSE_INDICATOR = "***FIXED_RESPONSE"


class PassthroughLLM(AugmentedLLM):
    """
    A specialized LLM implementation that simply passes through input messages without modification.

    This is useful for cases where you need an object with the AugmentedLLM interface
    but want to preserve the original message without any processing, such as in a
    parallel workflow where no fan-in aggregation is needed.
    """

    def __init__(self, name: str = "Passthrough", **kwargs: dict[str, Any]) -> None:
        super().__init__(name=name, **kwargs)
        self.provider = "fast-agent"
        self.logger = get_logger(__name__)
        self._messages = [PromptMessage]
        self._fixed_response: str | None = None

    async def generate(
        self,
        message: Union[str, MessageParamT, List[MessageParamT]],
        request_params: Optional[RequestParams] = None,
    ) -> Union[List[MessageT], Any]:
        """Simply return the input message as is."""
        # Return in the format expected by the caller
        return [message] if isinstance(message, list) else message

    async def generate_str(
        self,
        message: Union[str, MessageParamT, List[MessageParamT]],
        request_params: Optional[RequestParams] = None,
    ) -> str:
        """Return the input message as a string."""
        # Check if this is a special command to call a tool
        if isinstance(message, str) and message.startswith("***CALL_TOOL "):
            return await self._call_tool_and_return_result(message)

        self.show_user_message(message, model="fastagent-passthrough", chat_turn=0)
        await self.show_assistant_message(message, title="ASSISTANT/PASSTHROUGH")

        # Handle PromptMessage by concatenating all parts
        if isinstance(message, PromptMessage):
            parts_text = []
            for part in message.content:
                parts_text.append(str(part))
            return "\n".join(parts_text)

        return str(message)

    async def initialize(self) -> None:
        pass

    async def _call_tool_and_return_result(self, command: str) -> str:
        """
        Call a tool based on the command and return its result as a string.

        Args:
            command: The command string, expected format: "***CALL_TOOL <server>-<tool_name> [arguments_json]"

        Returns:
            Tool result as a string
        """
        try:
            tool_name, arguments = self._parse_tool_command(command)
            result = await self.aggregator.call_tool(tool_name, arguments)
            return self._format_tool_result(tool_name, result)
        except Exception as e:
            self.logger.error(f"Error calling tool: {str(e)}")
            return f"Error calling tool: {str(e)}"

    def _parse_tool_command(self, command: str) -> tuple[str, Optional[dict]]:
        """
        Parse a tool command string into tool name and arguments.

        Args:
            command: The command string in format "***CALL_TOOL <tool_name> [arguments_json]"

        Returns:
            Tuple of (tool_name, arguments_dict)

        Raises:
            ValueError: If command format is invalid
        """
        parts = command.split(" ", 2)
        if len(parts) < 2:
            raise ValueError("Invalid format. Expected '***CALL_TOOL <tool_name> [arguments_json]'")

        tool_name = parts[1].strip()
        arguments = None

        if len(parts) > 2:
            try:
                arguments = json.loads(parts[2])
            except json.JSONDecodeError:
                raise ValueError(f"Invalid JSON arguments: {parts[2]}")

        self.logger.info(f"Calling tool {tool_name} with arguments {arguments}")
        return tool_name, arguments

    def _format_tool_result(self, tool_name: str, result) -> str:
        """
        Format tool execution result as a string.

        Args:
            tool_name: The name of the tool that was called
            result: The result returned from the tool

        Returns:
            Formatted result as a string
        """
        if result.isError:
            error_text = []
            for content_item in result.content:
                if hasattr(content_item, "text"):
                    error_text.append(content_item.text)
                else:
                    error_text.append(str(content_item))
            error_message = "\n".join(error_text) if error_text else "Unknown error"
            return f"Error calling tool '{tool_name}': {error_message}"

        result_text = []
        for content_item in result.content:
            if hasattr(content_item, "text"):
                result_text.append(content_item.text)
            else:
                result_text.append(str(content_item))

        return "\n".join(result_text)

    async def generate_structured(
        self,
        message: Union[str, MessageParamT, List[MessageParamT]],
        response_model: Type[ModelT],
        request_params: Optional[RequestParams] = None,
    ) -> ModelT | None:
        """
        Return the input message as the requested model type.
        This is a best-effort implementation - it may fail if the
        message cannot be converted to the requested model.
        """
        if isinstance(message, response_model):
            return message
        elif isinstance(message, dict):
            return response_model(**message)
        elif isinstance(message, str):
            return response_model.model_validate(from_json(message, allow_partial=True))

    async def _apply_prompt_provider_specific(
        self,
        multipart_messages: List["PromptMessageMultipart"],
        request_params: RequestParams | None = None,
    ) -> PromptMessageMultipart:
        last_message = multipart_messages[-1]

        # TODO -- improve when we support Audio/Multimodal gen
        if self.is_tool_call(last_message):
            return Prompt.assistant(await self.generate_str(last_message.first_text()))

        if last_message.first_text().startswith(FIXED_RESPONSE_INDICATOR):
            self._fixed_response = (
                last_message.first_text().split(FIXED_RESPONSE_INDICATOR, 1)[1].strip()
            )

        if self._fixed_response:
            return Prompt.assistant(self._fixed_response)
        else:
            concatenated: str = "\n".join(message.all_text() for message in multipart_messages)
            return Prompt.assistant(concatenated)

    def is_tool_call(self, message: PromptMessageMultipart) -> bool:
        return message.first_text().startswith(CALL_TOOL_INDICATOR)
