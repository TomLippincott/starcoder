import pickle
import re
import sys
import argparse
import torch
import json
import numpy
import scipy.sparse
import gzip
from torch.utils.data import DataLoader, Dataset
import functools
import numpy
import random
import logging
from torch.optim import Adam, SGD
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import torch.nn.functional as F
from starcoder.fields import NumericField, DistributionField, CategoricalField, SequentialField, IntegerField, DateField
from starcoder.models import SingleSummarizer, Autoencoder, MLPProjector
from starcoder.registry import field_model_classes, summarizer_classes, projector_classes

logger = logging.getLogger(__name__)

class GraphAutoencoder(torch.nn.Module):
    def __init__(self,
                 schema,
                 depth,
                 autoencoder_shapes,
                 reverse_relations=False,
                 summarizers=SingleSummarizer,
                 activation=torch.nn.ReLU,
                 projected_size=None,
                 base_entity_representation_size=8,
                 device=torch.device("cpu")):
        """
        """
        super(GraphAutoencoder, self).__init__()
        self.reverse_relations = reverse_relations
        self.schema = schema
        self.depth = depth
        self.device = device
        self.base_entity_representation_size = base_entity_representation_size        
        self.autoencoder_shapes = autoencoder_shapes
        self.bottleneck_size = None if autoencoder_shapes in [[], None] else autoencoder_shapes[-1]
        
        # An encoder for each field that turns its data type into a fixed-size representation
        self.field_encoders = {}
        for field_name, field_object in self.schema.data_fields.items():
            field_type = type(field_object)
            if field_type not in field_model_classes:
                raise Exception("There is no encoder architecture registered for field type '{}'".format(field_type))
            self.field_encoders[field_name] = field_model_classes[field_type][0](field_object, activation)
        self.field_encoders = torch.nn.ModuleDict(self.field_encoders)

        # The size of an encoded entity is the sum of the base representation size and the encoded sizes of its possible fields        
        self.boundary_sizes = {}
        for entity_type in self.schema.entity_types.values():
            self.boundary_sizes[entity_type.name] = self.base_entity_representation_size
            for field_name in entity_type.data_fields:
                self.boundary_sizes[entity_type.name] += self.field_encoders[field_name].output_size

        # An autoencoder for each entity type and depth
        # The first has input/output layers of size equal to the size of the corresponding entity's representation size
        # The rest have input/output layers of that size plus a bottleneck size for each possible (normal or reverse) relation
        self._entity_autoencoders = {}
        for entity_type in self.schema.entity_types.values():
            boundary_size = self.boundary_sizes[entity_type.name]
            self._entity_autoencoders[entity_type.name] = [Autoencoder([boundary_size] + self.autoencoder_shapes, activation)]
            for _ in entity_type.relation_fields:
                boundary_size += self.bottleneck_size
            if self.reverse_relations:
                for _ in entity_type.reverse_relation_fields:
                    boundary_size += self.bottleneck_size
            for depth in range(self.depth):
                self._entity_autoencoders[entity_type.name].append(Autoencoder([boundary_size] + self.autoencoder_shapes, activation))
            self._entity_autoencoders[entity_type.name] = torch.nn.ModuleList(self._entity_autoencoders[entity_type.name])
        self._entity_autoencoders = torch.nn.ModuleDict(self._entity_autoencoders)

        # A summarizer for each relation particant (source or target), to reduce one-to-many relations to a fixed size
        if self.depth > 0:
            self.relation_source_summarizers = {}
            self.relation_target_summarizers = {}
            for relation_field in self.schema.relation_fields.values():
                self.relation_source_summarizers[relation_field.name] = summarizers(self.bottleneck_size, activation)
                self.relation_target_summarizers[relation_field.name] = summarizers(self.bottleneck_size, activation)
            self.relation_source_summarizers = torch.nn.ModuleDict(self.relation_source_summarizers)
            self.relation_target_summarizers = torch.nn.ModuleDict(self.relation_target_summarizers)

        # MLP for each entity type to project representations to a common size
        # note the largest boundary size
        self.projected_size = projected_size if projected_size != None else max(self.boundary_sizes.values())
        self._projectors = {}
        for entity_type in self.schema.entity_types.values():
            boundary_size = self.boundary_sizes.get(entity_type.name, 0)
            if self.depth > 0:
                for _ in entity_type.relation_fields:
                    boundary_size += self.bottleneck_size
                if self.reverse_relations:
                    for _ in entity_type.reverse_relation_fields:
                        boundary_size += self.bottleneck_size
            self._projectors[entity_type.name] = MLPProjector(boundary_size, self.projected_size, activation)
        self._projectors = torch.nn.ModuleDict(self._projectors)
        
        # A decoder for each field that takes a projected representation and generates a value of the field's data type
        self._field_decoders = {}
        self.field_losses = {}
        for field_name, field_object in self.schema.data_fields.items():
            field_type = type(field_object)
            self._field_decoders[field_name] = field_model_classes[field_type][1](field_object,
                                                                                  self.projected_size,
                                                                                  activation)
            self.field_losses[field_name] = field_model_classes[field_type][2](field_object)
        self._field_decoders = torch.nn.ModuleDict(self._field_decoders)

    def cuda(self, name="cuda:0"):
        self.device = torch.device(name)
        super(GraphAutoencoder, self).cuda()

    @property
    def parameter_count(self):
        return sum(p.numel() for p in self.parameters() if p.requires_grad)
        
    def forward(self, entities, adjacencies):
        logger.debug("Starting forward pass")
        num_entities = len(entities[self.schema.id_field.name])
        entity_indices = {}
        entity_masks = {}
        field_indices = {}
        field_masks = {}
        entity_field_indices = {}
        entity_field_masks = {}
        autoencoder_boundary_pairs = []

        rev_adjacencies = {k : v.T for k, v in adjacencies.items()}
        
        logger.debug("Assembling entity, field, and (entity, field) indices")        
        index_space = torch.arange(0, entities[self.schema.entity_type_field.name].shape[0], 1, device=self.device)
        for field in self.schema.data_fields.values():
            # FIXME: hack for RNNs            
            if field.type_name in ["sequential", "text"]:
                if entities[field.name].shape[1] == 0:
                    field_masks[field.name] = torch.full((entities[field.name].shape[0],), False, device=self.device, dtype=torch.bool)
                else:
                    field_masks[field.name] = entities[field.name][:, 0] != 0
            else:
                field_masks[field.name] = ~torch.isnan(torch.reshape(entities[field.name], (entities[field.name].shape[0], -1)).sum(1))
            field_indices[field.name] = index_space.masked_select(field_masks[field.name])
            
        for entity_type in self.schema.entity_types.values():
            entity_masks[entity_type.name] = torch.tensor((entities[self.schema.entity_type_field.name] == entity_type.name), device=self.device)
            entity_indices[entity_type.name] = index_space.masked_select(entity_masks[entity_type.name])
            for field_name in entity_type.data_fields:
                entity_field_masks[(entity_type.name, field_name)] = entity_masks[entity_type.name] & field_masks[field_name]
                entity_field_indices[(entity_type.name, field_name)] = index_space.masked_select(entity_field_masks[(entity_type.name, field_name)])
                
        logger.debug("Encoding each input field to a fixed-length representation")
        field_encodings = {}
        for field in self.schema.data_fields.values():
            field_encodings[field.name] = torch.full(size=(num_entities, self.field_encoders[field.name].output_size),
                                                     fill_value=0.0,
                                                     device=self.device)
            indices = field_indices[field.name]
            if len(indices) > 0:
                field_values = torch.index_select(entities[field.name], 0, indices)
                field_encodings[field.name][indices] = self.field_encoders[field.name](field_values).to(device=self.device)

        logger.debug("Constructing entity-autoencoder inputs by concatenating field encodings")
        autoencoder_inputs = {}
        for entity_type in self.schema.entity_types.values():
            #
            # each appended value should have shape (entity_count x encoding_width)
            #
            autoencoder_inputs[entity_type.name] = [torch.zeros(size=(entity_indices[entity_type.name].shape[0], 8), dtype=torch.float32, device=self.device)]
            for field_name in entity_type.data_fields:
                autoencoder_inputs[entity_type.name].append(torch.index_select(field_encodings[field_name], 0, entity_indices[entity_type.name]))
            autoencoder_inputs[entity_type.name].append(torch.zeros(size=(entity_indices[entity_type.name].shape[0], 0), dtype=torch.float32, device=self.device))
            autoencoder_inputs[entity_type.name] = torch.cat(autoencoder_inputs[entity_type.name], 1)

        # always holds the most-recent autoencoder reconstructions
        autoencoder_outputs = {}

        # always holds the most-recent bottleneck representations
        bottlenecks = torch.zeros(size=(num_entities, self.bottleneck_size), device=self.device)

        # zero-depth autoencoder
        depth = 0
        logger.debug("Running %d-depth autoencoder", depth)
        for entity_type in self.schema.entity_types.values():
            entity_outputs, bns, losses = self._entity_autoencoders[entity_type.name][0](autoencoder_inputs[entity_type.name])
            if entity_outputs != None:
                autoencoder_outputs[entity_type.name] = entity_outputs
            if bns != None:
                bottlenecks[entity_indices[entity_type.name]] = bns

        # n-depth autoencoders
        prev_bottlenecks = bottlenecks.clone()
        for depth in range(1, self.depth + 1):
            logger.debug("Running %d-depth autoencoder", depth)
            for entity_type in self.schema.entity_types.values():
                autoencoder_outputs[entity_type.name] = autoencoder_outputs[entity_type.name].narrow(1, 0, self._entity_autoencoders[entity_type.name][0].output_size)
                other_reps = []
                for rel_name in entity_type.relation_fields:
                    summarize = self.relation_target_summarizers[rel_name]
                    relation_reps = torch.zeros(size=(len(entity_indices[entity_type.name]), self.bottleneck_size), device=self.device)
                    for i, index in enumerate(entity_indices[entity_type.name]):
                        if rel_name not in adjacencies:
                            continue
                        related_indices = index_space.masked_select(adjacencies[rel_name][index])
                        if len(related_indices) > 0:
                            obns = torch.index_select(prev_bottlenecks, 0, related_indices)
                            relation_reps[i] = summarize(obns)
                    other_reps.append(relation_reps)
                if self.reverse_relations:
                    for rel_name in entity_type.reverse_relation_fields:
                        summarize = self.relation_source_summarizers[rel_name]
                        relation_reps = torch.zeros(size=(len(entity_indices[entity_type.name]), self.bottleneck_size), device=self.device)
                        for i, index in enumerate(entity_indices[entity_type.name]):
                            if rel_name not in adjacencies:
                                continue
                            related_indices = index_space.masked_select(adjacencies[rel_name][index])
                            if len(related_indices) > 0:
                                obns = torch.index_select(prev_bottlenecks, 0, related_indices)
                                relation_reps[i] = summarize(obns)
                        other_reps.append(relation_reps)
                sh = list(autoencoder_outputs[entity_type.name].shape)
                sh[1] = 0
                other_reps = torch.cat(other_reps, 1) if len(other_reps) > 0 else torch.zeros(size=tuple(sh), device=self.device)
                autoencoder_inputs[entity_type.name] = torch.cat([autoencoder_outputs[entity_type.name], other_reps], 1)
                if depth > len(self._entity_autoencoders[entity_type.name]) - 1:
                    logger.debug("At depth %d, while the model was trained for depth %d, so reusing final autoencoder",
                                 depth + 1,
                                 len(self._entity_autoencoders[entity_type.name]))
                    entity_outputs, bns, losses = self._entity_autoencoders[entity_type.name][-1](autoencoder_inputs[entity_type.name])
                else:
                    entity_outputs, bns, losses = self._entity_autoencoders[entity_type.name][depth](autoencoder_inputs[entity_type.name])
                autoencoder_outputs[entity_type.name] = entity_outputs
                if entity_outputs.shape[1] != 0:
                    bottlenecks[entity_indices[entity_type.name]] = bns
                    
                
        logger.debug("Projecting autoencoder outputs so entities have the same representation size")
        resized_autoencoder_outputs = torch.zeros(size=(num_entities, self.projected_size), device=self.device)
        for entity_type_name, ae_output in autoencoder_outputs.items():
            indices = entity_indices[entity_type_name]
            resized_autoencoder_outputs[indices] = self._projectors[entity_type_name](ae_output)
        
        logger.debug("Reconstructing the input by applying decoders to the autoencoder output")
        reconstructions = {}
        for field in self.schema.data_fields.values():
            reconstructions[field.name] = self._field_decoders[field.name](resized_autoencoder_outputs)
        reconstructions[self.schema.id_field.name] = entities[self.schema.id_field.name]
        reconstructions[self.schema.entity_type_field.name] = entities[self.schema.entity_type_field.name]

        logger.debug("Returning reconstructions, bottlenecks, and autoencoder I/O pairs")
        return (reconstructions, bottlenecks, autoencoder_boundary_pairs)

    # Recursively initialize model weights
    def init_weights(m):
        if type(m) == torch.nn.Linear or type(m) == torch.nn.Conv1d:
            torch.nn.init.xavier_uniform_(m.weight)
            m.bias.data.fill_(0.01)
    
