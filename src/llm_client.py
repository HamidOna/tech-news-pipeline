"""Unified LLM client with Groq primary and Google Gemini fallback."""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import google.generativeai as genai
from groq import AsyncGroq, RateLimitError as GroqRateLimitError

from src.db import get_connection

logger = logging.getLogger(__name__)


@dataclass
class LLMConfig:
    primary_model: str = "llama-3.3-70b-versatile"
    fallback_model: str = "gemini-2.0-flash"
    max_retries: int = 3
    retry_base_delay: float = 2.0
    temperature: float = 0.7
    max_tokens: int = 512


class LLMClient:
    """Async LLM client: primary Groq, fallback Google Gemini."""

    def __init__(self, config: Optional[LLMConfig] = None) -> None:
        self.config = config or LLMConfig()
        self._groq = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        self._gemini_model = genai.GenerativeModel(self.config.fallback_model)

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Send a prompt to the LLM. Tries Groq first, falls back to Gemini.

        Returns the generated text content.
        """
        # Try primary (Groq)
        try:
            return await self._groq_complete(system_prompt, user_prompt)
        except GroqRateLimitError:
            logger.warning("Groq rate limited, falling back to Gemini")
        except Exception as e:
            logger.warning("Groq request failed (%s), falling back to Gemini", e)

        # Fallback (Gemini)
        return await self._gemini_complete(system_prompt, user_prompt)

    async def _groq_complete(self, system_prompt: str, user_prompt: str) -> str:
        """Call Groq with retries and exponential backoff."""
        last_error: Optional[Exception] = None

        for attempt in range(self.config.max_retries):
            try:
                response = await self._groq.chat.completions.create(
                    model=self.config.primary_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                )
                text = response.choices[0].message.content or ""
                logger.info("LLM response via Groq (attempt %d)", attempt + 1)
                return text.strip()

            except GroqRateLimitError:
                # Don't retry rate limits — escalate to fallback immediately
                raise

            except Exception as e:
                last_error = e
                delay = self.config.retry_base_delay * (2 ** attempt)
                logger.warning(
                    "Groq attempt %d failed: %s. Retrying in %.1fs",
                    attempt + 1, e, delay,
                )
                await asyncio.sleep(delay)

        raise last_error or RuntimeError("Groq failed after retries")

    async def _gemini_complete(self, system_prompt: str, user_prompt: str) -> str:
        """Call Google Gemini (sync SDK wrapped in executor)."""
        combined_prompt = f"{system_prompt}\n\n{user_prompt}"

        def _call() -> str:
            response = self._gemini_model.generate_content(
                combined_prompt,
                generation_config=genai.GenerationConfig(
                    temperature=self.config.temperature,
                    max_output_tokens=self.config.max_tokens,
                ),
            )
            return response.text.strip()

        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, _call)
        logger.info("LLM response via Gemini (fallback)")
        return text
