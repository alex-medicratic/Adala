import openai
import pandas as pd
import re

from pydantic import BaseModel
from typing import List, Optional, Any, Dict, Tuple, Union
from abc import ABC, abstractmethod
from pydantic import Field, model_validator

from typing import Optional
from adala.runtimes.base import LLMRuntime
from adala.datasets import Dataset, DataFrameDataset
from adala.runtimes.base import Runtime
from adala.memories.base import Memory
from adala.utils.internal_data import InternalDataFrame, InternalDataFrameConcat
from adala.utils.logs import print_error


class BaseSkill(BaseModel, ABC):
    """
    A foundational abstract class representing a skill. This class sets the foundation 
    for all skills and provides common attributes and methods for skill-based operations.

    Attributes:
        name (str): Unique name of the skill.
        instructions (str): Instructs agent what to do with the input data.
        description (str): Description of the skill.
        input_template (str): Template for the input data.
        output_template (str): Template for the output data.
        input_data_field (str): Name of the input data field.
        prediction_field (str): Name of the prediction field to be used for the output data.
    """
    name: str = Field(
        title='Skill name',
        description='Unique name of the skill',
        default='',
        examples=['labeling', 'classification', 'text-generation']
    )
    instructions: str = Field(
        title='Skill instructions',
        description='Instructs agent what to do with the input data. '
                    'Can use templating to refer to input fields.',
        default='',
        examples=['Label the input text with the following labels: {{labels}}']
    )
    description: Optional[str] = Field(
        default='',
        title='Skill description',
        description='Description of the skill. Can be used to retrieve skill from the library.',
        examples=['The skill to perform sentiment analysis on the input text.']
    )
    input_template: Optional[str] = Field(
        title='Input template',
        description='Template for the input data. '
                    'Can use templating to refer to input parameters and perform data transformations.',
        default="Input: {{{{{input}}}}}",
        examples=["Text: {{{{{input}}}}}, Date: {{{{date_column}}}}, Sentiment: {{{{gen 'sentiment'}}}}"]
    )
    # TODO: skill can have multiple input fields
    input_data_field: Optional[str] = Field(
        title='Input data field',
        description='Input data field name that will be used to match input data.',
        examples=['text'],
        # TODO: either make it required, or `input_template` required
        default=None
    )
    output_template: Optional[str] = Field(
        title='Output template',
        description='Template for the output data. '
                    'Can use templating to refer to input parameters and perform data transformations',
        default="Output: {{gen 'predictions'}}",
        examples=["Output: {{select 'predictions' options=labels logprobs='score'}}"]
    )
    prediction_field: Optional[str] = Field(
        title='Prediction field',
        description='Prediction field name that will be used to match ground truth labels.'
                    'Should match at least one output field in `output_template`, e.g. \'predictions\'',
        examples=['predictions'],
        default='predictions'
    )

    @model_validator(mode='after')
    def validate_inputs(self) -> 'BaseSkill':
        """
        Validates the input_template, updating it if necessary.
        
        Returns:
            BaseSkill: Updated instance of the BaseSkill class.
        """
        if '{{{{{input}}}}}' in self.input_template:
            if self.input_data_field is None:
                print_error(f'You provided skill "{self.name}" with input template:\n\n'
                            f'{self.__class__.__name__}.input_template = "{self.input_template}"\n\n'
                            'that contains "{{{{{input}}}}}" placeholder. (yes... 5 curly braces!) \n\n'
                            'In this case, you have to provide skill with `skill.input_data_field` to match the input data.'
                            f'\nFor example, if your input data stored in `"text"` column, '
                            f'you can set\n\nskill = {self.__class__.__name__}(..., input_data_field="text")')
                raise ValueError(f'`input_data_field` is not provided for skill {self.name}')
            self.input_template = self.input_template.format(input=self.input_data_field)
        return self

    def __call__(self, input: InternalDataFrame, runtime: Runtime, dataset: Dataset) -> InternalDataFrame:
        """Calls the runtime to process a batch of inputs. Input and
        output shapes can be varying, and it should also take care of
        data types validation

        Args:
            input (InternalDataFrame): Input data in the form of an InternalDataFrame.
            runtime (Runtime): The runtime instance to be used for processing.
            dataset (Dataset): The dataset containing the data to be processed.
        
        Returns:
            InternalDataFrame: Concatenated dataframe with the original input and the predictions from the runtime.

        """

        # get user defined dataset input fields

        runtime_predictions = runtime.process_batch(
            batch=input,
            input_template=self.input_template,
            output_template=self.output_template,
            instructions=self.instructions,
            extra_fields=self._get_extra_fields()
        )
        runtime_predictions.rename(columns={self.prediction_field: self.name}, inplace=True)
        output = input.copy()
        output[runtime_predictions.columns] = runtime_predictions[runtime_predictions.columns]
        return output

    def _get_extra_fields(self):
        """
        Retrieves fields that are not categorized as system fields.
        
        Returns:
            dict: A dictionary containing fields that are not system fields.
        """
        
        # TODO: more robust way to exclude system fields
        system_fields = {
            'name', 'description', 'input_template', 'output_template', 'instructions',
            'input_data_field', 'prediction_field'}
        extra_fields = self.model_dump(exclude=system_fields)
        return extra_fields

    @abstractmethod
    def apply(
        self, dataset: Dataset,
        runtime: Runtime,
    ) -> InternalDataFrame:
        """
        Applies the skill to a dataset and returns the results.
        
        Args:
            dataset (Dataset): The dataset on which the skill is to be applied.
            runtime (Runtime): The runtime instance to be used for processing.

        Returns:
            ShortTermMemory: The updated experience after applying the skill.
        """        

    @abstractmethod
    def analyze(
        self,
        predictions: InternalDataFrame,
        errors: InternalDataFrame,
        student_runtime: Runtime,
        teacher_runtime: Optional[Runtime] = None,
        memory: Optional[Memory] = None,
    ) -> str:
        """
        Analyzes the results to derive new experiences.
        It gets provided skill predictions in the format:

        ```markdown
        | input | skill_1 | skill_2 | skill_3 |
        |-------|---------|---------|---------|
        | text1 | label11 | label21 | label31 |
        | text2 | label12 | label22 | label32 |
        | ...   | ...     | ...     | ...     |
        ```

        and the errors for a specific skill to analyze in the format:

        ```markdown
        | prediction | ground_truth |
        |------------|--------------|
        | label11    | label12      |
        | ...        | ...          |
        ```

        and returns the string that contains the error analysis report.
        
        Args:
            predictions (InternalDataFrame): The predictions made by the skill.
            errors (InternalDataFrame): The errors made by the skill.
            student_runtime (Runtime): The runtime instance used to get predictions.
            teacher_runtime (Optional[Runtime]): The runtime instance to be used for analysing the errors.
            memory (Optional[Memory]): The memory instance to be used for processing.

        Returns:
            str: The error analysis report.
        """

    @abstractmethod
    def improve(
        self,
        error_analysis: str,
        runtime: Runtime
    ):
        """
        Refines the LLM skill based on its recent experiences and updates the skill's instructions.

        Args:
            error_analysis (str): The error analysis report.
            runtime (Runtime): The runtime instance to be used for processing.
        """


class LLMSkill(BaseSkill):
    """
    A skill specialized for Language Models (LLM). Inherits from the BaseSkill 
    class and provides specific implementations for handling LLM predictions based 
    on given instructions.

    Attributes:
        name (str): Unique name of the skill.
        instructions (str): Instructs agent what to do with the input data.
        description (str): Description of the skill.
        input_template (str): Template for the input data.
        output_template (str): Template for the output data.
        input_data_field (str): Name of the input data field.
        prediction_field (str): Name of the prediction field to be used for the output data.
    """

    def apply(
        self,
        dataset: Union[Dataset, InternalDataFrame],
        runtime: LLMRuntime,
    ) -> InternalDataFrame:
        """
        Applies the LLM skill on a dataset and returns the results.
        
        Args:
            dataset (Union[Dataset, InternalDataFrame]): The dataset on which the skill is to be applied.
            runtime (LLMRuntime): The runtime instance to be used for processing.

        Returns:
            predictions (InternalDataFrame): The predictions made by the skill.
        """

        predictions = []
        if isinstance(dataset, InternalDataFrame):
            dataset = DataFrameDataset(df=dataset)

        for batch in dataset.batch_iterator():
            runtime_predictions = self(batch, runtime, dataset)
            predictions.append(runtime_predictions)

        if predictions:
            return InternalDataFrameConcat(predictions, copy=False)

        return InternalDataFrame(columns=dataset.df.columns.tolist() + [self.name])

    def analyze(
        self,
        predictions: InternalDataFrame,
        errors: InternalDataFrame,
        student_runtime: Runtime,
        teacher_runtime: Optional[Runtime] = None,
        memory: Optional[Memory] = None
    ) -> str:
        """
        Analyzes the results to identify any discrepancies and returns the observed experience.
        
        Args:
            predictions (InternalDataFrame): The predictions made by the skill.
            errors (InternalDataFrame): The errors made by the skill.
            student_runtime (Runtime): The runtime instance used to get predictions.
            teacher_runtime (Optional[Runtime]): The runtime instance to be used for analysing the errors.
            memory (Optional[Memory]): The memory instance to be used for processing.

        Returns:
            str: The error analysis report.
        """

        # collect errors and create error report
        # first sample errors - make it uniform, but more sophisticated sampling can be implemented
        MAX_ERRORS = 3
        errors = errors.sample(n=min(MAX_ERRORS, errors.shape[0]))
        # TODO: ground truth column name can be the input parameter that comes from GT signal
        ground_truth_column_name = errors.columns[-1]
        extra_fields = self._get_extra_fields()

        # get error prepared inputs
        inputs = student_runtime.process_batch(
            batch=predictions.loc[errors.index],
            input_template=self.input_template,
            extra_fields=extra_fields
        )

        if not teacher_runtime:
            teacher_runtime = student_runtime

        predictions_and_errors = pd.concat([
            inputs,
            predictions[self.name].loc[errors.index],
            errors[ground_truth_column_name]
        ], axis=1)
        predictions_and_errors.columns = ['input', 'prediction', 'ground_truth']
        # TODO: move handlebars to Runtime level and abstract template language for skill
        # For example, using f-string format as generic, that translates to handlebars inside GuidanceRuntime
        error_reasons = teacher_runtime.process_batch(
            batch=predictions_and_errors,
            instructions="{{#system~}}\n"
                         "LLM prompt was created by concatenating instructions with text input:\n\n"
                         "Prediction = LLM(Input, Instructions)\n\n"
                         "We expect the prediction to be equal to the ground truth.\n"
                         "Your task is to provide a reason for the error due to the original instruction.\n"
                         "Be concise and specific.\n\n"
                         f"Instructions: {self.instructions}\n"
                         "{{~/system}}",
            input_template="{{#user~}}\n"
                           "{{input}}\n"
                           "Prediction: {{prediction}}\n"
                           "Ground truth: {{ground_truth}}\n"
                           "Error reason:\n"
                           "{{~/user}}",
            output_template="{{#assistant~}}{{gen 'reason'}}{{~/assistant}}",
            extra_fields=extra_fields
        )
        predictions_and_errors['reason'] = error_reasons['reason']
        # build error report
        result = teacher_runtime.process_record(
            record={
                'predictions_and_errors': predictions_and_errors.to_dict(orient='records'),
            },
            input_template="{{#each predictions_and_errors}}"
                           "\n{{this.input}}\n"
                           "Prediction: {{this.prediction}}\n"
                           "Ground truth: {{this.ground_truth}}\n"
                           'Error reason: {{this.reason}}\n'
                           "{{/each}}"
        )
        # no specific output specified, all output is in the error report
        error_report = result['']
        return error_report

    def improve(
        self,
        error_analysis: str,
        runtime: Runtime,
    ):
        """
        Refines the LLM skill based on its recent experiences and updates the skill's instructions.

        Args:
            error_analysis (str): The error analysis report.
            runtime (Runtime): The runtime instance to be used for processing.
        """

        result = runtime.process_record(
            record={
                'error_analysis': error_analysis
            },
            instructions="{{#system~}}\n"
                         "LLM prompt was created by concatenating instructions with text input:\n\n"
                         "Prediction = LLM(Input, Instructions)\n\n"
                         "We expect the prediction to be equal to the ground truth.\n"
                         "Your task is to analyze errors made by old instructions "
                         "and craft new instructions for the LLM.\n"
                         "Follow best practices for LLM prompt engineering.\n"
                         "Include 2-3 examples at the end of your response to demonstrate how the new instruction would be applied.\n"
                         "Use the following format for your examples:\n"
                         "Input: ...\n"
                         "Output: ...\n\n"
                         "{{~/system}}\n",
            input_template="{{#user~}}\n"
                           f"Old instructions: {self.instructions}\n\n"
                           "Errors:\n{{error_analysis}}\n"
                           "New instruction:\n"
                           "{{~/user}}",
            output_template="{{#assistant~}}{{gen 'new_instruction'}}{{~/assistant}}",
            extra_fields=self._get_extra_fields()
        )
        self.instructions = result['new_instruction']
