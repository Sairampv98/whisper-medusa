"""
Some of the following code is a snippet with changes from https://github.com/FasterDecoding/Medusa/blob/e2a5d20c048a9b0a4092e6933c34313687422518/medusa/model/utils_legacy.py
"""

from typing import Optional

import torch
from transformers.generation import GenerationConfig
from transformers.generation.logits_process import (
    LOGITS_PROCESSOR_INPUTS_DOCSTRING, LogitsProcessor)
from transformers.utils import add_start_docstrings


class MedusaGenerationConfig(GenerationConfig):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.posterior_threshold = kwargs.pop("posterior_threshold", 0.09)
        self.posterior_alpha = kwargs.pop("posterior_alpha", 0.3)


class MedusaWhisperTimeStampLogitsProcessor(LogitsProcessor):
    r"""

    [`LogitsProcessor`] that modifies the logits for the generation of timestamps in the transcription. When the input
    tokens are at a specific threshold, the processor sets the scores to negative infinity. The processor makes sure
    that timestamp tokens appear in pairs, by masking out the logits that would break this pairing pattern. This is
    done to maintain the consistency and structure of generated timestamps. It also ensures that when the predicted
    probability of sampling any of the timestamp token is greater than any individual non-timestamp token, those
    non-timestamp logits are set to negative infinity. This is done to ensure the generation of timestamps over other
    potential tokens.


    See [the paper](https://arxiv.org/abs/2212.04356) for more information.

    Args:
        generate_config (`GenerateConfig`):
            The generate config used to generate the output. The following parameters are required:
                eos_token_id (`int`, *optional*, defaults to 50257):
                    The id of the *end-of-sequence* token.
                no_timestamps_token_id (`int`, *optional*, defaults to 50363):
                    The id of the `"<|notimestamps|>"` token.
                max_initial_timestamp_index (`int`, *optional*, defaults to 1):
                    Used to set the maximum value of the initial timestamp. This is used to prevent the model from
                    predicting timestamps that are too far in the future.
        begin_index (`Optional`, *optional*): Token index of the first token that is generated by the model.
        _detect_timestamp_from_logprob (`bool`, *optional*): Whether timestamps can be predicted from logprobs over all timestamps.

    Examples:
    ``` python
    >>> import torch
    >>> from transformers import AutoProcessor, WhisperForConditionalGeneration, GenerationConfig
    >>> from datasets import load_dataset

    >>> processor = AutoProcessor.from_pretrained("openai/whisper-tiny.en")
    >>> model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-tiny.en")
    >>> ds = load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")
    >>> inputs = processor(ds[3]["audio"]["array"], return_tensors="pt")
    >>> input_features = inputs.input_features

    >>> #Displaying timestamps
    >>> generated_ids = model.generate(inputs=input_features, return_timestamps=True)
    >>> transcription = processor.batch_decode(generated_ids, decode_with_timestamps=True)[0]
    >>> print("Transcription:", transcription)
    Transcription: <|startoftranscript|><|0.00|> He has grave doubts whether Sir Frederick Layton's work is really Greek after all, and can<|6.44|><|6.44|> discover in it but little of rocky Ithaca.<|9.44|><|endoftext|>


    >>> #No timestamps & change EOS:
    >>> #This allows the user to select a specific token to terminate the sequence on, in this case it's the word "can"(460)
    >>> model.generation_config.eos_token_id = 460
    >>> generated_ids = model.generate(inputs=input_features,return_timestamps=False)
    >>> transcription = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
    >>> print("Transcription:", transcription)
    Transcription:  He has grave doubts whether Sir Frederick Layton's work is really Greek after all and can
    ```
    """

    def __init__(
        self,
        generate_config,
        begin_index: Optional[int] = None,
        _detect_timestamp_from_logprob: Optional[bool] = None,
    ):  # support for the kwargs
        self.no_timestamps_token_id = generate_config.no_timestamps_token_id
        self.timestamp_begin = generate_config.no_timestamps_token_id + 1
        self.eos_token_id = generate_config.eos_token_id or generate_config.bos_token_id

        # this variable is mostly just used for testing
        self._detect_timestamp_from_logprob = (
            _detect_timestamp_from_logprob
            if _detect_timestamp_from_logprob is not None
            else getattr(generate_config, "_detect_timestamp_from_logprob", True)
        )

        num_forced_ids = (
            len(generate_config.forced_decoder_ids)
            if generate_config.forced_decoder_ids is not None
            else 0
        )
        self.begin_index = begin_index or (num_forced_ids + 1)

        self.max_initial_timestamp_index = getattr(
            generate_config, "max_initial_timestamp_index", None
        )

    def set_begin_index(self, begin_index):
        self.begin_index = begin_index

    @add_start_docstrings(LOGITS_PROCESSOR_INPUTS_DOCSTRING)
    def __call__(
        self, input_ids: torch.LongTensor, scores: torch.FloatTensor
    ) -> torch.FloatTensor:
        # suppress <|notimestamps|> which is handled by without_timestamps
        scores[:, :, self.no_timestamps_token_id] = -float("inf")
        num_medusa_heads = scores.shape[1]
        # timestamps have to appear in pairs, except directly before eos_token; mask logits accordingly
        for k in range(input_ids.shape[0]):
            sampled_tokens = input_ids[k, self.begin_index :]
            seq = list(sampled_tokens.tolist())

            last_was_timestamp = len(seq) >= 1 and seq[-1] >= self.timestamp_begin
            penultimate_was_timestamp = len(seq) < 2 or seq[-2] >= self.timestamp_begin

            if last_was_timestamp:
                if penultimate_was_timestamp:  # has to be non-timestamp
                    scores[k, 0, self.timestamp_begin :] = -float("inf")
                else:  # cannot be normal text tokens
                    scores[k, 0, : self.eos_token_id] = -float("inf")
            timestamps = sampled_tokens[sampled_tokens.ge(self.timestamp_begin)]
            if timestamps.numel() > 0:
                # `timestamps` shouldn't decrease; forbid timestamp tokens smaller than the last
                # The following lines of code are copied from: https://github.com/openai/whisper/pull/914/files#r1137085090
                if last_was_timestamp and not penultimate_was_timestamp:
                    timestamp_last = timestamps[-1]
                else:
                    # Avoid to emit <|0.00|> again
                    timestamp_last = timestamps[-1] + 1

                scores[k, :, : self.timestamp_begin : timestamp_last] = -float("inf")

        # apply the `max_initial_timestamp` option
        if input_ids.shape[1] == self.begin_index:
            scores[:, 0, : self.timestamp_begin] = -float("inf")
            scores[:, 1, self.timestamp_begin :] = -float("inf")
            if self.max_initial_timestamp_index is not None:
                last_allowed = self.timestamp_begin + self.max_initial_timestamp_index
                scores[:, 0, last_allowed + 1 :] = -float("inf")

        # if sum of probability over timestamps is above any other token, sample timestamp
        logprobs = torch.nn.functional.log_softmax(scores.float(), dim=-1)
        for k in range(input_ids.shape[0]):
            medusa_timestamp_logprob = logprobs[k, :, self.timestamp_begin :].logsumexp(
                dim=-1
            )
            medusa_max_text_token_logprob = torch.max(
                logprobs[k, :, : self.timestamp_begin], axis=-1
            ).values
            prev_wav_updated = False
            for i in range(len(medusa_timestamp_logprob)):
                timestamp_logprob = medusa_timestamp_logprob[i]
                max_text_token_logprob = medusa_max_text_token_logprob[i]
                if (
                    timestamp_logprob > max_text_token_logprob or prev_wav_updated
                ) and self._detect_timestamp_from_logprob:
                    if prev_wav_updated:
                        prev_wav_updated = False
                    else:
                        prev_wav_updated = True
                    scores[k, i, : self.timestamp_begin] = -float("inf")

        return scores


class MedusaSuppressTokensLogitsProcessor(LogitsProcessor):
    r"""
    This processor can be used to suppress a list of tokens in multi heads prediction. The processor will set their log probs to `-inf` so
    that they are not generated. Originally created for
    [Whisper](https://huggingface.co/docs/transformers/model_doc/whisper).

    """

    def __init__(self, suppress_tokens):
        self.suppress_tokens = list(suppress_tokens)

    @add_start_docstrings(LOGITS_PROCESSOR_INPUTS_DOCSTRING)
    def __call__(
        self, input_ids: torch.LongTensor, scores: torch.FloatTensor
    ) -> torch.FloatTensor:
        """
        scores: torch.LongTensor with shape of [num_heads, batch_size, seq_len]
        """
        scores[:, :, self.suppress_tokens] = -float("inf")
        return scores


class MedusaSuppressTokensAtBeginLogitsProcessor(LogitsProcessor):
    r"""
    [`SuppressTokensAtBeginLogitsProcessor`] supresses a list of tokens as soon as the `generate` function starts
    generating using `begin_index` tokens for multi heads prediction. This should ensure that the tokens defined by `begin_suppress_tokens` are
    not generated at the begining. Originally created for
    [Whisper](https://huggingface.co/docs/transformers/model_doc/whisper).

    Examples:

    ```python
    >>> from transformers import AutoProcessor, WhisperForConditionalGeneration
    >>> from datasets import load_dataset

    >>> processor = AutoProcessor.from_pretrained("openai/whisper-tiny.en")
    >>> model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-tiny.en")
    >>> ds = load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")
    >>> inputs = processor(ds[0]["audio"]["array"], return_tensors="pt")

    >>> # Whisper has `begin_suppress_tokens` set by default (= `[220, 50256]`). 50256 is the EOS token, so this means
    >>> # it can't generate and EOS token in the first iteration, but it can in the others.
    >>> outputs = model.generate(**inputs, return_dict_in_generate=True, output_scores=True)
    >>> print(outputs.scores[1][0, 50256])  # 1 (and not 0) is the first freely generated token
    tensor(-inf)
    >>> print(outputs.scores[-1][0, 50256])  # in other places we can see some probability mass for EOS
    tensor(29.9010)

    >>> # If we disable `begin_suppress_tokens`, we can generate EOS in the first iteration.
    >>> outputs = model.generate(
    ...     **inputs, return_dict_in_generate=True, output_scores=True, begin_suppress_tokens=None
    ... )
    >>> print(outputs.scores[1][0, 50256])
    tensor(11.2027)
    ```
    """

    def __init__(self, begin_suppress_tokens, begin_index):
        self.begin_suppress_tokens = list(begin_suppress_tokens)
        self.begin_index = begin_index

    def set_begin_index(self, begin_index):
        self.begin_index = begin_index

    @add_start_docstrings(LOGITS_PROCESSOR_INPUTS_DOCSTRING)
    def __call__(
        self, input_ids: torch.LongTensor, scores: torch.FloatTensor
    ) -> torch.FloatTensor:
        if input_ids.shape[1] == self.begin_index:
            scores[:, :, self.begin_suppress_tokens] = -float("inf")

        return scores


class MedusaWhisperNoSpeechDetection(LogitsProcessor):
    r"""This processor can be used to detect silence when using Whisper. It should take as input unprocessed logits to follow the original implementation"""

    def __init__(
        self, no_speech_token: int, begin_index: int, scores_is_logprobs: bool = False
    ):
        self.no_speech_token = no_speech_token
        # offset between <start-of-transcription> token, <SOT>, in paper and first generated token
        # is equal to the position of the first generated token index
        self.start_of_trans_offset = begin_index

        # `self.begin_index` is a running value that is changed on the fly
        self.begin_index = begin_index
        self._no_speech_prob = [0.0]
        self.is_scores_logprobs = scores_is_logprobs

        # overwritten dynamically
        self.model = None
        self.inputs = None

    def set_model(self, model):
        self.model = model

    def set_inputs(self, inputs):
        self.inputs = {**self.model.prepare_inputs_for_generation(**inputs), **inputs}
        self.inputs["input_features"] = self.inputs.pop("inputs")

    @property
    def no_speech_prob(self):
        return self._no_speech_prob

    def set_begin_index(self, begin_index):
        self.begin_index = begin_index

    @add_start_docstrings(LOGITS_PROCESSOR_INPUTS_DOCSTRING)
    def __call__(
        self, input_ids: torch.LongTensor, scores: torch.FloatTensor
    ) -> torch.FloatTensor:
        if input_ids.shape[1] == self.begin_index:
            if self.start_of_trans_offset > 1:
                with torch.no_grad():
                    logits = self.model(**self.inputs).logits

                no_speech_index = self.begin_index - self.start_of_trans_offset
                no_speech_scores = logits[:, :, no_speech_index]
            else:
                no_speech_scores = scores

            if self.is_scores_logprobs:
                probs = no_speech_scores.exp()
            else:
                probs = no_speech_scores.float().softmax(dim=-1)

            self._no_speech_prob = probs[:, :, self.no_speech_token]

        return scores


def generate_medusa_buffers(medusa_choices, device="cuda"):
    """
    Generate buffers related to the Medusa structure.
    Split each part for readability.

    Explanation of each buffer in the returned dictionary:
    1. tree_indices: Represents indices that map items from a linear list to a tree structure.
    2. medusa_attn_mask: The attention mask designed specifically for the Medusa structure, ensuring proper attention computation.
    3. medusa_position_ids: Denotes the position identifiers used within the Medusa structure.
    4. retrieve_indices: Provides indices that map items from a tree structure back to their original positions in a cartesian product.
    5. list_indices: Represents indices mapping items from a tree back to a list. This is intended for a future feature and is currently under testing.

    Args:
        medusa_choices (torch.Tensor): A tensor containing choices for the Medusa structure.
        device (str, optional): Target device for the generated buffers. Defaults to "cuda".

    Returns:
        dict: A dictionary containing several buffer tensors for the Medusa structure.
    """
    # NOTE - notice this assumes greedy decoding for the original logits!
    medusa_choices = torch.tensor(medusa_choices)
    cumulative_product = torch.cumprod(medusa_choices, dim=0)
    cumulative_sum = torch.cumsum(medusa_choices, dim=0)
    medusa_len = cumulative_product.sum().item()
    medusa_attn_mask = torch.eye(medusa_len, medusa_len)

    # 1. Generate tree indices based on the Medusa choices
    medusa_indices = torch.arange(cumulative_sum[-1])
    tree_indices = []
    prev_cumsum = 0
    prev_cumprod = 1
    for i in range(medusa_choices.size(0)):
        cumsum = cumulative_sum[i].item()
        cumprod = cumulative_product[i].item()
        slice = medusa_indices[prev_cumsum:cumsum].repeat(prev_cumprod, 1).flatten()
        tree_indices += slice.tolist()
        prev_cumsum = cumsum
        prev_cumprod = cumprod

    # 2. Update the Medusa attention mask
    prev_cumprod_sum = -1
    for i in range(medusa_choices.size(0)):
        cumprod_sum = cumulative_product[:i].sum().item()
        if prev_cumprod_sum != -1:
            parent_idx = (
                torch.arange(prev_cumprod_sum, cumprod_sum)
                .repeat(medusa_choices[i], 1)
                .transpose(0, 1)
                .flatten()
            )
            medusa_attn_mask[
                cumprod_sum : cumprod_sum + parent_idx.size(0)
            ] += medusa_attn_mask[parent_idx]
        prev_cumprod_sum = cumulative_product[:i].sum().item()

    # 3. Generate Medusa position IDs
    medusa_position_ids = []
    for i in range(medusa_choices.size(0)):
        medusa_position_ids += [i] * cumulative_product[i]

    # 4. Generate retrieval indices for Medusa structure verification
    medusa_len_prod = torch.prod(medusa_choices).item()
    retrieve_indices = torch.zeros(
        medusa_len_prod, len(medusa_choices), dtype=torch.long
    )
    prev_cumprod_sum = 0
    for i in range(medusa_choices.size(0)):
        cumprod_sum = cumulative_product[: i + 1].sum().item()
        retrieve_indices[:, i] = (
            torch.arange(prev_cumprod_sum, cumprod_sum)
            .repeat(medusa_len_prod // (cumprod_sum - prev_cumprod_sum), 1)
            .transpose(0, 1)
            .flatten()
        )
        prev_cumprod_sum = cumprod_sum

    # 5. Generate list indices for Medusa structure
    list_indices = []
    cumulative_product = torch.cumprod(medusa_choices, dim=0)
    cumulative_product_max = torch.max(cumulative_product)
    prev_cumprod_sum = 0

    for i in range(medusa_choices.size(0)):
        current_indices = torch.arange(
            prev_cumprod_sum, prev_cumprod_sum + medusa_choices[i]
        )
        current_indices = current_indices.repeat(
            cumulative_product[i] // medusa_choices[i], 1
        ) + torch.arange(cumulative_product[i] // medusa_choices[i]).unsqueeze(
            -1
        ) * current_indices.size(
            0
        )
        current_indices = current_indices.repeat(
            cumulative_product_max // (cumulative_product[i] // medusa_choices[i]), 1
        )
        list_indices.append(current_indices)
        prev_cumprod_sum += cumulative_product[i]
    list_indices = torch.cat(list_indices, dim=1).transpose(0, 1)

    # Compile all the buffers into a dictionary
    ret = {
        "medusa_attn_mask": medusa_attn_mask.unsqueeze(0).unsqueeze(0),
        "tree_indices": tree_indices,
        "medusa_position_ids": medusa_position_ids,
        "retrieve_indices": retrieve_indices,
        "list_indices": list_indices,
    }

    # Convert all items in the dictionary to tensors and move them to the specified device
    ret = {
        k: v.clone().to(device)
        if isinstance(v, torch.Tensor)
        else torch.tensor(v, device=device)
        for k, v in ret.items()
    }
    return ret


def reset_past_key_values(passed_key_values):
    """
    Resets the current lengths in the passed key-values to zero.

    This function is designed to be used during the evaluation of a baseline model.
    It iterates through each layer's key-values and sets their current lengths to zero,
    effectively resetting their state.

    Args:
    - passed_key_values (list of torch.Tensor): Contains past hidden states and past attention values for each layer.

    Returns:
    - passed_key_values (list of torch.Tensor): Updated past hidden states and past attention values with reset lengths.
    """
    for i in range(len(passed_key_values)):
        for j in range(2):
            passed_key_values[i][j].current_length.fill_(0)
    return passed_key_values


def generate_candidates(medusa_logits, logits, medusa_topk, tree_indices):
    """
    Generates candidate tokens based on the Medusa logits and original logits.

    This function performs a greedy decoding on the original logits to retrieve
    the most likely token. For the Medusa logits, it retrieves the top-k tokens
    as specified by the `medusa_topk` argument. Finally, the function reshapes
    and matches these candidates based on the tree structure defined by `tree_indices`.

    Args:
    - medusa_logits (torch.Tensor): Output tensor of shape (medusa, batch_size, vocabulary_size)
      representing the logits from Medusa layers.
    - logits (torch.Tensor): Original logits tensor of shape (batch_size, sequence_length, vocabulary_size).
    - medusa_topk (list of int): Contains the number of top-k tokens to consider for each Medusa layer.
    - tree_indices (list or torch.Tensor): Index mapping from a flattened list to tree structure.

    Returns:
    - candidates (torch.Tensor): Cartesian product of candidate tokens across Medusa layers.
    - tree_candidates (torch.Tensor): Reshaped candidates matched to the tree structure.
    """
    # NOTE - this assumes greedy decoding and beam of 1 for the original logits!
    # Greedy decoding for original logits
    candidates = [torch.argmax(logits[:, -1]).unsqueeze(0)]
    if len(set(medusa_topk)) == 1:
        # Retrieve top-k tokens for Medusa logits
        candidate_i = torch.topk(medusa_logits[:, 0, -1], medusa_topk[0]).indices
        candidates.extend(list(candidate_i))
    else:
        for i in range(medusa_logits.shape[0]):
            candidate_i = torch.topk(medusa_logits[i, 0, -1], medusa_topk[i]).indices
            candidates.append(candidate_i)
    candidates_flat = torch.cat(candidates)
    candidates = torch.cartesian_prod(*candidates)
    tree_candidates = candidates_flat[tree_indices].unsqueeze(0)
    return candidates, tree_candidates


def tree_decoding(
    model,
    tree_candidates,
    past_key_values,
    medusa_position_ids,
    input_ids,
    retrieve_indices,
    output_attentions,
    output_hidden_states,
    model_kwargs,
):
    """
    Decodes the token sequences using a tree-based approach with Medusa layers.

    Given the candidates for token sequences and the current past key values, the function
    decodes the sequences using the model's Medusa layers and retrieves the logits
    corresponding to the desired positions in the sequence.

    Args:
    - model (nn.Module): The main model with Medusa layers.
    - tree_candidates (torch.Tensor): Candidate tokens for the current decoding step based on the tree structure.
    - past_key_values (list of torch.Tensor): List of past key-value states to use for autoregressive decoding.
    - medusa_position_ids (list or torch.Tensor): Position IDs for the Medusa structure.
    - input_ids (torch.Tensor): The input token sequences of shape (batch_size, sequence_length).
    - retrieve_indices (list or torch.Tensor): Indices mapping from tree to cartesian product, used to reorder the logits.

    Returns:
    - medusa_logits (torch.Tensor): Medusa logits corresponding to the current decoding step.
    - logits (torch.Tensor): Original logits for the current step.
    - outputs (tuple): Intermediate model outputs.
    """

    # Compute new position IDs based on the Medusa structure and current input sequence length
    position_ids = medusa_position_ids + input_ids.shape[1]
    # Decode the tree candidates using the model
    if "past_key_values" in model_kwargs:
        model_kwargs["past_key_values"] = past_key_values
        tree_candidates_model_inputs = model.prepare_inputs_for_medusa_tree_generation(
            tree_candidates, **model_kwargs, decoder_position_ids=position_ids
        )
    else:
        tree_candidates_model_inputs = model.prepare_inputs_for_medusa_tree_generation(
            tree_candidates,
            **model_kwargs,
            past_key_values=past_key_values,
            decoder_position_ids=position_ids,
        )
    # NOTE - this code is true only when past_key_values are used in the model! (use_cache = true)!!!
    # forward pass to get next token
    outputs = model(
        **tree_candidates_model_inputs,
        return_dict=True,
        output_attentions=output_attentions,
        output_hidden_states=output_hidden_states,
        disable_medusa=True,  # there is no need to run medusa here
    )

    orig_tree_logits = outputs.logits[0]

    # Reorder the logits based on the retrieve_indices for consistency
    logits = orig_tree_logits[0, retrieve_indices]

    return logits, outputs


def evaluate_posterior(
    logits, candidates, temperature, posterior_threshold, posterior_alpha
):
    """
    Evaluate the posterior probabilities of the candidates based on the provided logits and choose the best candidate.

    Depending on the temperature value, the function either uses greedy decoding or evaluates posterior
    probabilities to select the best candidate.

    Args:
    - logits (torch.Tensor): Predicted logits of shape (batch_size, sequence_length, vocab_size).
    - candidates (torch.Tensor): Candidate token sequences.
    - temperature (float): Softmax temperature for probability scaling. A value of 0 indicates greedy decoding.
    - posterior_threshold (float): Threshold for posterior probability.
    - posterior_alpha (float): Scaling factor for the threshold.

    Returns:
    - best_candidate (torch.Tensor): Index of the chosen best candidate.
    - accept_length (int): Length of the accepted candidate sequence.
    """
    # Greedy decoding based on temperature value
    if temperature == 0:
        # Find the tokens that match the maximum logits for each position in the sequence
        posterior_mask = (
            candidates[:, 1:] == torch.argmax(logits[:, :-1], dim=-1)
        ).int()
        candidates_accept_length = (torch.cumprod(posterior_mask, dim=1)).sum(dim=1)
        accept_length = candidates_accept_length.max()
        # Choose the best candidate
        if accept_length == 0:
            # Default to the first candidate if none are accepted
            best_candidate = torch.tensor(0, dtype=torch.long, device=candidates.device)
        else:
            best_candidate = torch.argmax(candidates_accept_length).to(torch.long)
        return best_candidate, accept_length
    # Calculate posterior probabilities and thresholds for candidate selection
    posterior_prob = torch.softmax(logits[:, :-1] / temperature, dim=-1)
    candidates_prob = torch.gather(
        posterior_prob, dim=-1, index=candidates[:, 1:].unsqueeze(-1)
    ).squeeze(-1)
    posterior_entropy = -torch.sum(
        posterior_prob * torch.log(posterior_prob + 1e-5), dim=-1
    )  # torch.sum(torch.log(*)) is faster than torch.prod
    threshold = torch.minimum(
        torch.ones_like(posterior_entropy) * posterior_threshold,
        torch.exp(-posterior_entropy) * posterior_alpha,
    )
    posterior_mask = candidates_prob > threshold
    candidates_accept_length = (torch.cumprod(posterior_mask, dim=1)).sum(dim=1)

    # Choose the best candidate based on the evaluated posterior probabilities
    accept_length = candidates_accept_length.max()
    if accept_length == 0:
        # If no candidates are accepted, just choose the first one
        best_candidate = torch.tensor(0, dtype=torch.long, device=candidates.device)
    else:
        best_candidates = torch.where(candidates_accept_length == accept_length)[0]
        # Accept the best one according to likelihood
        likelihood = torch.sum(
            torch.log(candidates_prob[best_candidates, :accept_length]), dim=-1
        )
        best_candidate = best_candidates[torch.argmax(likelihood)]
    return best_candidate, accept_length


def update_inference_inputs(
    model,
    input_ids,
    candidates,
    best_candidate,
    accept_length,
    retrieve_indices,
    outputs,
    tree_outputs,
    logits,
    new_token,
    eos_token_id,
    pad_token_id,
    unfinished_sequences,
    use_base_logits,
):
    """
    Update the input sequences and relevant tensors based on the selected best candidate from the inference results.

    Args:
    - model (nn.Module): The main model with Medusa layers.
    - input_ids (torch.Tensor): Current input token sequences.
    - candidates (torch.Tensor): Candidate token sequences generated in the current step.
    - best_candidate (int): Index of the chosen best candidate.
    - accept_length (int): Length of the accepted candidate sequence.
    - retrieve_indices (torch.Tensor): Indices to map tree to a cartesian product.
    - outputs, whisper outputs
    - tree_outputs, tree outputs
    - new_token (int): Counter for the new tokens added during inference.
    - eos_token_id: The token ID for the end-of-sequence token.
    - pad_token_id: The token ID for the padding token.
    - unfinished_sequences (torch.Tensor): Binary tensor indicating unfinished sequences

    Returns:
    - input_ids (torch.Tensor): Updated input token sequences.
    - logits (torch.Tensor): Updated logits.
    - new_token (int): Updated counter for the new tokens added.
    """
    # Calculate the starting position for new tokens based on the previous input length
    prev_input_len = input_ids.shape[1]
    prev_indices = torch.arange(prev_input_len).to(input_ids.device)
    # Map the best candidate indices to the original indices in the sequence
    selected_tree_indices = retrieve_indices[best_candidate, : accept_length + 1]
    select_indices = selected_tree_indices + prev_input_len
    # Append the tokens from the best candidate to the input sequence
    next_tokens = candidates[None, best_candidate, : accept_length + 1]
    if use_base_logits:
        additional_next_token = torch.argmax(logits[:, 0], dim=-1)
        next_tokens = torch.cat(
            [next_tokens, additional_next_token.unsqueeze(0)], dim=-1
        )
    # finished sentences should have their next token be a padding token
    if eos_token_id is not None:
        if pad_token_id is None:
            raise ValueError(
                "If `eos_token_id` is defined, make sure that `pad_token_id` is defined."
            )
        next_tokens = next_tokens * unfinished_sequences + pad_token_id * (
            1 - unfinished_sequences
        )

    input_ids = torch.cat([input_ids, next_tokens], dim=-1)
    model._update_medusa_outputs(
        outputs,
        tree_outputs,
        select_indices,
        selected_tree_indices,
        accept_length,
        prev_indices,
        use_base_logits,
    )

    # Extract logits and medusa logits for the accepted tokens
    logits = logits[None, best_candidate, accept_length : accept_length + 1]
    # Update the new token counter
    if use_base_logits:
        new_token += accept_length + 2
    else:
        new_token += accept_length + 1

    return input_ids, logits, new_token, next_tokens
