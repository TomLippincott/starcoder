import numpy
import math
import time
import calendar
import torch
import logging

logger = logging.getLogger(__name__)

class Missing(object):
    pass

class Padding(object):
    pass

class NotApplicable(object):
    pass

class Field(object):
    """
Field objects represent a type with particular semantics and its canonical
representation.
    """
    def __init__(self, name, **args):
        self.name = name
        self.type_name = args["type"]
        self.empty = True
    def __str__(self):
        return "{1} field: {0}".format(self.name, self.type_name)
    def encode(self, v):
        return v
    def decode(self, v):
        return v
    def observe_value(self, v):        
        self.empty = False
        return self._observe_value(v)
    def _observe_value(self, v):
        return v
    
class MetaField(Field):
    def __init__(self, name, **args):
        super(MetaField, self).__init__(name, **args)
    def encode(self, v):
        return v
    def decode(self, v):
        return v
    def _observe_value(self, v):
        pass
        
class DataField(Field):        
    def __init__(self, name, **args):
        super(DataField, self).__init__(name, **args)
    
class EntityTypeField(MetaField):
    def __init__(self, name, **args):
        super(EntityTypeField, self).__init__(name, type="entity_type", **args)

class RelationField(MetaField):
    def __init__(self, name, **args):
        super(RelationField, self).__init__(name, **args)
        self.source_entity_type = args["source_entity_type"]        
        self.target_entity_type = args["target_entity_type"]
    def __str__(self):
        return "{}({}->{})".format(self.name, self.source_entity_type, self.target_entity_type)
    
class IdField(MetaField):
    def __init__(self, name, **args):
        super(IdField, self).__init__(name, type="id", **args)

class NumericField(DataField):
    encoded_type = torch.float32
    missing_value = float("nan")
    def __init__(self, name, **args):
        super(NumericField, self).__init__(name, **args)
        self.max_val = None
        self.min_val = None
    def _observe_value(self, v):        
        try:
            retval = float(v)
            self.max_val = retval if self.max_val == None else max(self.max_val, v)
            self.min_val = retval if self.min_val == None else min(self.min_val, v)
            return float(retval)
        except Exception as e:
            logger.error("Could not interpret '%s' for NumericField '%s'", v, self.name)
            raise e
    def decode(self, v):
        if isinstance(v, torch.Tensor):
            v = v.item()
        return (None if numpy.isnan(v) else v)
    def __str__(self):
        return "{1} field: {0}[{2}, {3}]".format(self.name, self.type_name, self.min_val, self.max_val)
    
class IntegerField(DataField):    
    def __init__(self, name, **args):
        super(IntegerField, self).__init__(name, **args)

    def __decode__(self, v):
        if isinstance(v, torch.Tensor):
            v = v.item()
        return v
        
class DateField(DataField):
    def __init__(self, name, **args):
        super(DateField, self).__init__(name, **args)
    def encode(self, v):
        t = time.strptime(v, "%d-%b-%Y")
        return calendar.timegm(t)
    def decode(self, v):
        date = time.gmtime(v)
        year = date.tm_year
        month = calendar.month_abbr[date.tm_mon]
        day = date.tm_mday
        return "{}-{}-{}".format(day, month, year)
    
class DistributionField(DataField):
    encoded_type = torch.float32
    missing_value = float("nan")
    def __init__(self, name, **args):
        super(DistributionField, self).__init__(name, **args)
        self.categories = []
        
    def encode(self, v):
        total = sum(v.values())
        return [0.0 if c not in v else (v[c] / total) for c in self.categories]

    def decode(self, v):
        retval = {}
        if all([x >= 0 for x in v]):
            total = sum([x for x in v])
            for k, p in zip(self.categories, v):
                if p > 0:
                    retval[k] = p / total
        elif all([x <= 0 for x in v]):            
            total = sum([math.exp(x) for x in v])
            for k, p in zip(self.categories, v):
                retval[k] = math.exp(p) / total            
        else:
            raise Exception("Got probabilities that were not all of the same sign!")
        return retval
    
    def _observe_value(self, v):
        for k, v in v.items():
            if k not in self.categories:
                self.categories.append(k)

class CategoricalField(DataField):
    missing_value = 0
    encoded_type = int
    def __init__(self, name, **args):
        super(CategoricalField, self).__init__(name, **args)
        self._lookup = {Missing() : self.missing_value}
        self._rlookup = {self.missing_value : Missing()}
         
    def _observe_value(self, v):
        i = self._lookup.setdefault(v, len(self._lookup))
        self._rlookup[i] = v
   
    def encode(self, v):
        return self._lookup.get(v, None) #self.unknown_value)

    def decode(self, v):
        if isinstance(v, torch.Tensor):
            if v.dtype == torch.int64:
                v = v.item()
            else:
                v = v.argmax().item()                
        if v not in self._rlookup:
            raise Exception("Could not decode value '{0}' (type={2})".format(v, self._rlookup, type(v)))
        return self._rlookup[v]
    
    def __str__(self):
        return "{1} field: {0}[{2}]".format(self.name, self.type_name, len(self._lookup))

    def __len__(self):
        return len(self._lookup)


    
class SequentialField(DataField):
    missing_value = ()
    
    encoded_type = int
    def __init__(self, name, **args):
        super(SequentialField, self).__init__(name, **args)
        self._lookup = {None : 0}
        self._rlookup = {0 : None}
        self.max_length = 0
        #unique_sequences = set()
        #self[Missing] = Missing.value
        #self[Unknown] = Unknown.value
        #self._max_length = 0
        #for value in field_values:
        #    unique_sequences.add(value)
        #    self._max_length = max(self._max_length, len(value))
        #    for element in value:
        #        self[element] = self.get(element, len(self))
        #self._rlookup = {v : k for k, v in self.items()}
        #self._unique_sequence_count = len(unique_sequences)

    #def __str__(self):
    #    return "Sequential(unique_elems={}, unique_seqs={}, max_length={})".format(len(self),
    #                                                                               self._unique_sequence_count, 
    #                                                                               self._max_length)
    def _observe_value(self, vs):
        for v in vs:
            i = self._lookup.setdefault(v, len(self._lookup))
            self._rlookup[i] = v
        self.max_length = max(len(vs), self.max_length)
            
    def __str__(self):
        return "{1} field: {0}[{2} values, {3} max length]".format(self.name, self.type_name, len(self._lookup), self.max_length)

    def encode(self, v):
        retval = [self._lookup[e] for e in v]
        return retval

    def decode(self, v):
        try:
            return "".join([self._rlookup[e] for e in v if e not in [Missing.value, Unknown.value]])
        except:
            raise Exception("Could not decode values '{0}' (type={2})".format(v, self._rlookup, type(v[0])))

    def __len__(self):
        return len(self._lookup)

# class WordField(DataField):
#     missing_value = ()    
#     encoded_type = int
#     def __init__(self, name, **args):
#         super(WordField, self).__init__(name, **args)
#         self._lookup = {None : 0}
#         self._rlookup = {0 : None}
#         self.max_length = 0
#     def observe_value(self, vs):
#         for v in vs:
#             i = self._lookup.setdefault(v, len(self._lookup))
#             self._rlookup[i] = v
#         self.max_length = max(len(vs), self.max_length)
#     def __str__(self):
#         return "{1} field: {0}[{2} values, {3} max length]".format(self.name, self.type_name, len(self._lookup), self.max_length)
#     def encode(self, v):
#         retval = [self._lookup[e] for e in v[0:10]]
#         return retval
#     def decode(self, v):
#         try:
#             return "".join([self._rlookup[e] for e in v if e not in [Missing.value, Unknown.value]])
#         except:
#             raise Exception("Could not decode values '{0}' (type={2})".format(v, self._rlookup, type(v[0])))
#     def __len__(self):
#         return len(self._lookup)

class CharacterField(DataField):
    missing_value = ()    
    encoded_type = int
    def __init__(self, name, **args):
        super(CharacterField, self).__init__(name, **args)
        self._lookup = {None : 0}
        self._rlookup = {0 : None}
        self.max_observed_length = 0
    def _observe_value(self, vs):
        for v in vs:
            i = self._lookup.setdefault(v, len(self._lookup))
            self._rlookup[i] = v
        self.max_observed_length = max(len(vs), self.max_observed_length)
    def __str__(self):
        return "{1} field: {0}[{2} values, {3} max length]".format(self.name, self.type_name, len(self._lookup), self.max_observed_length)
    def encode(self, v):
        retval = [self._lookup[e] for e in v]
        return retval
    def decode(self, v):
        try:
            return "".join([self._rlookup[e] for e in v if e not in [Missing.value, Unknown.value]])
        except:
            raise Exception("Could not decode values '{0}' (type={2})".format(v, self._rlookup, type(v[0])))
    def __len__(self):
        return len(self._lookup)
