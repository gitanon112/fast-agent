import contextlib
from enum import Enum
from typing import Callable, List, Optional, Type, TYPE_CHECKING
from pydantic import BaseModel, Field

from mcp_agent.workflows.llm.augmented_llm import (
    AugmentedLLM,
    MessageParamT,
    MessageT,
    ModelT,
    RequestParams,
)
from mcp_agent.agents.agent import Agent
from mcp_agent.logging.logger import get_logger

if TYPE_CHECKING:
    from mcp_agent.context import Context

logger = get_logger(__name__)


class QualityRating(str, Enum):
    """Enum for evaluation quality ratings"""

    POOR = 0  # Major improvements needed
    FAIR = 1  # Several improvements needed
    GOOD = 2  # Minor improvements possible
    EXCELLENT = 3  # No improvements needed


class EvaluationResult(BaseModel):
    """Model representing the evaluation result from the evaluator LLM"""

    rating: QualityRating = Field(description="Quality rating of the response")
    feedback: str = Field(
        description="Specific feedback and suggestions for improvement"
    )
    needs_improvement: bool = Field(
        description="Whether the output needs further improvement"
    )
    focus_areas: List[str] = Field(
        default_factory=list, description="Specific areas to focus on in next iteration"
    )


class EvaluatorOptimizerLLM(AugmentedLLM[MessageParamT, MessageT]):
    """
    Implementation of the evaluator-optimizer workflow where one LLM generates responses
    while another provides evaluation and feedback in a refinement loop.

    This can be used either:
    1. As a standalone workflow with its own optimizer agent
    2. As a wrapper around another workflow (Orchestrator, Router, ParallelLLM) to add
       evaluation and refinement capabilities

    When to use this workflow:
    - When you have clear evaluation criteria and iterative refinement provides value
    - When LLM responses improve with articulated feedback
    - When the task benefits from focused iteration on specific aspects

    Examples:
    - Literary translation with "expert" refinement
    - Complex search tasks needing multiple rounds
    - Document writing requiring multiple revisions
    """

    def __init__(
        self,
        generator: Agent | AugmentedLLM,
        evaluator: str | Agent | AugmentedLLM,
        min_rating: QualityRating = QualityRating.GOOD,
        max_refinements: int = 3,
        llm_factory: Callable[[Agent], AugmentedLLM]
        | None = None,  # TODO: Remove legacy - factory should only be needed for str evaluator
        context: Optional["Context"] = None,
    ):
        """
        Initialize the evaluator-optimizer workflow.

        Args:
            generator: The agent/LLM/workflow that generates responses. Can be:
                     - An Agent that will be converted to an AugmentedLLM
                     - An AugmentedLLM instance
                     - An Orchestrator/Router/ParallelLLM workflow
            evaluator_agent: The agent/LLM that evaluates responses
            evaluation_criteria: Criteria for the evaluator to assess responses
            min_rating: Minimum acceptable quality rating
            max_refinements: Maximum refinement iterations
            llm_factory: Optional factory to create LLMs from agents
        """
        super().__init__(context=context)

        # Set up the optimizer
        self.name = generator.name
        self.llm_factory = llm_factory
        self.generator = generator
        self.evaluator = evaluator

        # TODO: Remove legacy - optimizer should always be an AugmentedLLM, no conversion needed
        if isinstance(generator, Agent):
            if not llm_factory:
                raise ValueError("llm_factory is required when using an Agent")

            # Only create new LLM if agent doesn't have one
            if hasattr(generator, "_llm") and generator._llm:
                self.generator_llm = generator._llm
            else:
                self.generator_llm = llm_factory(agent=generator)

            self.aggregator = generator
            self.instruction = (
                generator.instruction
                if isinstance(generator.instruction, str)
                else None
            )

        elif isinstance(generator, AugmentedLLM):
            self.generator_llm = generator
            self.aggregator = generator.aggregator
            self.instruction = generator.instruction

        else:
            raise ValueError(f"Unsupported optimizer type: {type(generator)}")

        self.history = self.generator_llm.history

        # Set up the evaluator
        if isinstance(evaluator, AugmentedLLM):
            self.evaluator_llm = evaluator
        # TODO: Remove legacy - evaluator should be either AugmentedLLM or str
        elif isinstance(evaluator, Agent):
            if not llm_factory:
                raise ValueError(
                    "llm_factory is required when using an Agent evaluator"
                )

            # Only create new LLM if agent doesn't have one
            if hasattr(evaluator, "_llm") and evaluator._llm:
                self.evaluator_llm = evaluator._llm
            else:
                self.evaluator_llm = llm_factory(agent=evaluator)
        elif isinstance(evaluator, str):
            # If a string is passed as the evaluator, we use it as the evaluation criteria
            # and create an evaluator agent with that instruction
            if not llm_factory:
                raise ValueError(
                    "llm_factory is required when using a string evaluator"
                )

            self.evaluator_llm = llm_factory(
                agent=Agent(name="Evaluator", instruction=evaluator)
            )
        else:
            raise ValueError(f"Unsupported evaluator type: {type(evaluator)}")

        self.min_rating = min_rating
        self.max_refinements = max_refinements

        # Track iteration history
        self.refinement_history = []

    async def generate(
        self,
        message: str | MessageParamT | List[MessageParamT],
        request_params: RequestParams | None = None,
    ) -> List[MessageT]:
        """Generate an optimized response through evaluation-guided refinement"""
        refinement_count = 0
        response = None
        best_response = None
        best_rating = QualityRating.POOR
        self.refinement_history = []

        # Use a single AsyncExitStack for the entire method to maintain connections
        async with contextlib.AsyncExitStack() as stack:
            # Enter all agent contexts once at the beginning
            if isinstance(self.generator, Agent):
                await stack.enter_async_context(self.generator)
            if isinstance(self.evaluator, Agent):
                await stack.enter_async_context(self.evaluator)

            # Initial generation
            response = await self.generator_llm.generate(
                message=message,
                request_params=request_params,
            )

            best_response = response

            while refinement_count < self.max_refinements:
                logger.debug("Optimizer result:", data=response)

                # Evaluate current response
                eval_prompt = self._build_eval_prompt(
                    original_request=str(message),
                    current_response="\n".join(str(r) for r in response)
                    if isinstance(response, list)
                    else str(response),
                    iteration=refinement_count,
                )

                # No need for nested AsyncExitStack here - using the outer one
                evaluation_result = await self.evaluator_llm.generate_structured(
                    message=eval_prompt,
                    response_model=EvaluationResult,
                    request_params=request_params,
                )

                # Track iteration
                self.refinement_history.append(
                    {
                        "attempt": refinement_count + 1,
                        "response": response,
                        "evaluation_result": evaluation_result,
                    }
                )

                logger.debug("Evaluator result:", data=evaluation_result)

                # Track best response (using enum ordering)
                if evaluation_result.rating.value > best_rating.value:
                    best_rating = evaluation_result.rating
                    best_response = response
                    logger.debug(
                        "New best response:",
                        data={"rating": best_rating, "response": best_response},
                    )

                # Check if we've reached acceptable quality
                if (
                    evaluation_result.rating.value >= self.min_rating.value
                    or not evaluation_result.needs_improvement
                ):
                    logger.debug(
                        f"Acceptable quality {evaluation_result.rating.value} reached",
                        data={
                            "rating": evaluation_result.rating.value,
                            "needs_improvement": evaluation_result.needs_improvement,
                            "min_rating": self.min_rating.value,
                        },
                    )
                    break

                # Generate refined response
                refinement_prompt = self._build_refinement_prompt(
                    original_request=str(message),
                    current_response="\n".join(str(r) for r in response)
                    if isinstance(response, list)
                    else str(response),
                    feedback=evaluation_result,
                    iteration=refinement_count,
                )

                # No nested AsyncExitStack here either
                response = await self.generator_llm.generate(
                    message=refinement_prompt,
                    request_params=request_params,
                )

                refinement_count += 1

            return best_response

    async def generate_str(
        self,
        message: str | MessageParamT | List[MessageParamT],
        request_params: RequestParams | None = None,
    ) -> str:
        """Generate an optimized response and return it as a string"""
        response = await self.generate(
            message=message,
            request_params=request_params,
        )

        # Handle case where response is a single message
        if not isinstance(response, list):
            return str(response)

        # Convert all messages to strings, handling different message types
        result_strings = []
        for r in response:
            if hasattr(r, "text"):
                result_strings.append(r.text)
            elif hasattr(r, "content"):
                # Handle ToolUseBlock and similar
                if isinstance(r.content, list):
                    # Typically content is a list of blocks
                    result_strings.extend(str(block) for block in r.content)
                else:
                    result_strings.append(str(r.content))
            else:
                # Fallback to string representation
                result_strings.append(str(r))

        return "\n".join(result_strings)

    async def generate_structured(
        self,
        message: str | MessageParamT | List[MessageParamT],
        response_model: Type[ModelT],
        request_params: RequestParams | None = None,
    ) -> ModelT:
        """Generate an optimized structured response"""
        response_str = await self.generate_str(
            message=message, request_params=request_params
        )

        return await self.generator.generate_structured(
            message=response_str,
            response_model=response_model,
            request_params=request_params,
        )

    def _build_eval_prompt(
        self, original_request: str, current_response: str, iteration: int
    ) -> str:
        """Build the evaluation prompt for the evaluator"""
        return f"""
You are an expert evaluator for content quality. Your task is to evaluate a response against the user's original request.

Evaluate the response for iteration {iteration + 1} and provide structured feedback on its quality and areas for improvement.

<fastagent:data>
<fastagent:request>
{original_request}
</fastagent:request>

<fastagent:response>
{current_response}
</fastagent:response>

<fastagent:evaluation-criteria>
{self.evaluator.instruction}
</fastagent:evaluation-criteria>
</fastagent:data>

<fastagent:instruction>
Provide a structured evaluation with the following components:

<rating>
Choose one: EXCELLENT, GOOD, FAIR, or POOR
- EXCELLENT: No improvements needed
- GOOD: Only minor improvements possible
- FAIR: Several improvements needed
- POOR: Major improvements needed
</rating>

<details>
Provide specific, actionable feedback and suggestions for improvement.
Be precise about what works well and what could be improved.
</details>

<needs_improvement>
Indicate true/false whether further improvement is needed.
</needs_improvement>

<focus-areas>
List 1-3 specific areas to focus on in the next iteration.
Be concrete and actionable in your recommendations.
</focus-areas>
</fastagent:instruction>
"""

    def _build_refinement_prompt(
        self,
        original_request: str,
        current_response: str,
        feedback: EvaluationResult,
        iteration: int,
    ) -> str:
        """Build the refinement prompt for the optimizer"""
        history_enabled = hasattr(self, "history") and self.history

        # Start with clear non-delimited instructions
        prompt = f"""
You are tasked with improving a response based on expert feedback. This is iteration {iteration + 1} of the refinement process.

Your goal is to address all feedback points while maintaining accuracy and relevance to the original request.
"""

        # Add data section with all relevant information
        prompt += """
<fastagent:data>
"""

        # Add request
        prompt += f"""
<fastagent:request>
{original_request}
</fastagent:request>
"""

        # Only include previous response if history is not enabled
        if not history_enabled:
            prompt += f"""
<fastagent:previous-response>
{current_response}
</fastagent:previous-response>
"""

        # Always include the feedback
        prompt += f"""
<fastagent:feedback>
<rating>{feedback.rating}</rating>
<details>{feedback.feedback}</details>
<focus-areas>{", ".join(feedback.focus_areas) if feedback.focus_areas else "None specified"}</focus-areas>
</fastagent:feedback>
</fastagent:data>
"""

        # Customize instruction based on history availability
        if not history_enabled:
            prompt += """
<fastagent:instruction>
Create an improved version of the response that:
1. Directly addresses each point in the feedback
2. Focuses on the specific areas mentioned for improvement
3. Maintains all the strengths of the original response
4. Remains accurate and relevant to the original request

Provide your complete improved response without explanations or commentary.
</fastagent:instruction>
"""
        else:
            prompt += """
<fastagent:instruction>
Your previous response is available in your conversation history.

Create an improved version that:
1. Directly addresses each point in the feedback
2. Focuses on the specific areas mentioned for improvement
3. Maintains all the strengths of your original response
4. Remains accurate and relevant to the original request

Provide your complete improved response without explanations or commentary.
</fastagent:instruction>
"""

        return prompt
