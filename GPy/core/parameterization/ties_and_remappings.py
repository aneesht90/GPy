# Copyright (c) 2014, James Hensman, Max Zwiessele
# Licensed under the BSD 3-clause license (see LICENSE.txt)

import numpy as np
from parameterized import Parameterized
from param import Param

class Remapping(Parameterized):
    def mapping(self):
        """
        The return value of this function gives the values which the re-mapped
        parameters should take. Implement in sub-classes.
        """
        raise NotImplementedError

    def callback(self):
        raise NotImplementedError

    def __str__(self):
        return self.name

    def parameters_changed(self):
        #ensure all out parameters have the correct value, as specified by our mapping
        index = self._highest_parent_.constraints[self]
        self._highest_parent_.param_array[index] = self.mapping()
        [p.notify_observers(which=self) for p in self.tied_parameters]

class Fix(Remapping):
    pass


class Tie(Parameterized):
    """
    The new parameter tie framework. (under development)
    
    All the parameters tied together get a new parameter inside the *Tie* object. 
    Its value should always be equal to all the tied parameters, and its gradient
    is the sum of all the tied parameters.
    
    =====Implementation Details=====
    The *Tie* object should only exist on the top of param tree (the highest parent).
    
    Each tied param object has the attribute _tie_ which stores the labels for tied parameters.
    
    self.label_buf:
    It uses a label buffer that has the same length as all the parameters (self._highest_parent_.param_array).
    The buffer keeps track of all the tied parameters. All the tied parameters have a label (an interger) higher 
    than 0, and the parameters that have the same label are tied together.
    
    self.buf_index:
    An auxiliary index list for the global index of the tie parameter inside the *Tie* object.
    
    ================================
    
    TODO:
    1. Add the support for multiple parameter tie_together and tie_vector [Preliminary]
    2. Properly handling parameters with constraints [DONE]
    3. Properly handling the merging of two models [DONE]
    4. Properly handling initialization [DONE]
    
    """
    def __init__(self, name='Ties'):
        # whether it has just propagated tied parameter values during optimization
        # If ture, it does not need to check consistency
        self._PROPAGATE_VAL_ = False
        super(Tie, self).__init__(name)
        self.tied_param = None
        # The buffer keeps track of tie status
        self.label_buf = None
        self.buf_idx = None
        self._untie_ = None
        
    @staticmethod
    def recoverTies(p):
        """Recover the Tie object from the param objects"""
        if not p.has_parent():
            p.ties = Tie()
            p.link_parameter(p.ties, -1)
            p.add_observer(p.ties, p.ties._parameters_changed_notification, priority=-500)
            
            p.update_model(False)
            labels = p.ties._get_labels([p])
            labels = labels[labels>0]
            if len(labels)>0:
                p._expand_tie_param(len(labels))
                vals = p.ties._get_sync_val(p, labels)
                p.tied_param[:] = vals
                p.tied_param.tie[:] = labels
            p._update_label_buf()
            p.update_model(True)

    def mergeTies(self, p):
        """Merge the tie tree with another tie tree"""
        assert hasattr(p,'ties') and isinstance(p.ties,Tie), str(type(p))
        #self.update_model(False)
        if p.ties.tied_param is not None:
            tie_labels,_ = self._expand_tie_param(p.ties.tied_param.size)
            self.tied_param[-p.ties.tied_param.size:] = p.ties.tied_param
            pairs = zip(self.tied_param.tie,tie_labels)
            self._replace_labels(p, pairs)
        p.remove_observer(p.ties)
        p.unlink_parameter(p.ties)
        del p.ties
        self._update_label_buf()
        #self.update_model(True)

    def splitTies(self, p):
        """Split the tie subtree from the original tie tree"""
        p.ties = Tie()
        p.link_parameter(p.ties, -1)
        p.add_observer(p.ties, p.ties._parameters_changed_notification, priority=-500)
        if self.tied_param is not None:
            self.update_model(False)
            labels = self._get_labels([p])
            labels = labels[labels>0]
            if len(labels)>0:
                p._expand_tie_param(len(labels))
                idx = np.in1d(self.tied_param.tie,labels)
                p.tied_param[:] = self.tied_param[idx]
                p.tied_param.tie[:] = self.tied_param.tie[idx]
            self._remove_unnecessary_ties()
            self._update_label_buf()
            p._update_label_buf()
            self.update_model(True)
            
    def _get_sync_val(self, p, labels):
        vals = np.empty((labels.size,))
        read = np.zeros((labels.size,),dtype=np.uint8)
        def _get_sync_v(p, labels, vals, read):
            for i in xrange(labels.size):
                if read[i]==1:
                    p[p.tie==labels[i]] = vals[i]
                elif np.any(p.tie==labels[i]):
                    vals[i] = p[p.tie==labels[i]][0]
                    p[p.tie==labels[i]][0] = vals[i]
                    read[i] = 1
        self._traverse_param(_get_sync_v, (p,labels,vals,read), [])
        return vals
                    

    def _sync_val_group(self, plist):
        val = np.hstack([p.param_array.flat for p in plist]).mean()
        def _set_val(p):
            p[:] = val
        for p in plist:
            self._traverse_param(_set_val, (p,), [])
        return val

    def _sync_constraint_group(self, plist, hastie=False, tie_con=None, warning=True):
        if not hastie:
            cons = []
            for p in plist:
                cons.extend(p.constraints.properties())
            cons = list(set(cons))
            if len(cons)==0:
                tie_con = None
            else:
                tie_con = cons[0]
        if tie_con is not None:
            for p in plist:
                if len(p.constraints.properties())!=1 or p.constraints[tie_con].size != p.size:
                    print 'WARNING: '+p.name+' have different constraints! They will be constrained '+str(tie_con)+'!'
                    p.constrain(tie_con)
            return tie_con
        elif hastie:
            for p in plist:
                if p.constraints.size>0:
                    print 'WARNING: '+p.name+' have different constraints! They will be unconstrained!'
                p.unconstrain()
        return None

    def _sync_constraint_vector(self, p1, p2, expandlist, idxlist, warning=True):
        if p1.constraints.items() != p2.constraints.properties():
            print 'WARNING: '+p1.name+' and '+p2.name+' have different constraints! Only the constraints of '+p1.name+' will be considered!'
        for c,ind in p1.constraints.iteritems():
            idx = idxlist[np.in1d(expandlist,ind)]
            self.tied_param[idx].constrain(c)
        
    def _traverse_param(self, func, p, res):
        """
        Traverse a param tree starting with *p*
        Apply *func* to every leaves (param objects),
        and collect return values into *res*
        """
        if isinstance(p[0], Param):
            res.append(func(*p))
        else:
            for pc in p[0].parameters:
                self._traverse_param(func, (pc,)+p[1:] ,res)

    def _get_labels(self, plist):
        labels = []
        for p in plist:
            self._traverse_param(lambda x: x.tie.flat, (p,), labels)
        return np.unique(np.hstack(labels))
    
    def _get_labels_vector(self, p1,p2):
        label1 = []
        self._traverse_param(lambda x: x.tie.flat, (p1,), label1)
        label1 = np.hstack(label1)
        label2 = []
        self._traverse_param(lambda x: x.tie.flat, (p2,), label2)
        label2 = np.hstack(label2)
        expandlist = np.where(label1*label2==0)[0]
        labellist =label1.copy()
        idx = np.logical_and(label1==0,label2>0)
        labellist[idx] = label2[idx]
        idx = np.logical_and(label1*label2>0,label1!=label2)
        removelist = (label1[idx],label2[idx])
        return expandlist,removelist,labellist
    
    def _set_labels(self, plist, labels):
        """
        If there is only one label, set all the param objects to that label,
        otherwise each parameter take a label.
        """
        def _set_l1(p):
            p.tie[:] = labels[0]
        def _set_list(p, offset):
            p.tie.flat[:] = labels[offset[0]:offset[0]+p.size]
            offset[0] = offset[0]+ p.size
        if len(labels)==1:
            for p in plist:
                self._traverse_param(_set_l1, (p,), [])
        else:
            for p in plist:
                self._traverse_param(_set_list, (p,[0]), [])
                
    def _get_vals(self, p):
        vals = []
        self._traverse_param(lambda x: x.flat, (p,), vals)
        return np.hstack(vals)
    
    def _sync_val_pair(self,p1,p2):
        p1val = self._get_vals(p1)
        def _set_val(p, offset, p2):
            p.flat[:] = p2[offset[0]:offset[0]+p.size]
            offset[0] = offset[0]+ p.size
        self._traverse_param(_set_val, (p2, [0], p1val), [])
        return p1val
    
    def _replace_labels(self, p, label_pairs):
        def _replace_l(p):
            for l1,l2 in label_pairs:
                p.tie[p.tie==l1] = l2
        self._traverse_param(_replace_l, (p,), [])

    def _expand_tie_param(self, num):
        """Expand the tie param with the number of *num* parameters"""
        if self.tied_param is None:
            start_label = 1
            labellist = np.array(range(start_label,start_label+num),dtype=np.int)
            idxlist = np.array(range(0,num),dtype=np.int)
            new_buf = np.empty((num,))
            self.tied_param = Param('tied',new_buf)
            self.tied_param.tie[:] = labellist
        else:
            start_label = self.tied_param.tie.max()+1
            new_buf = np.empty((self.tied_param.size+num,))
            new_buf[:self.tied_param.size] = self.tied_param.param_array.copy()
            old_tie_ = self.tied_param.tie.copy()
            old_size = self.tied_param.size
            labellist = np.array(range(start_label,start_label+num),dtype=np.int)
            idxlist = np.array(range(old_size,old_size+num),dtype=np.int)
            self.unlink_parameter(self.tied_param)
            self.tied_param = Param('tied',new_buf)
            self.tied_param.tie[:old_size] = old_tie_
            self.tied_param.tie[old_size:] = labellist
        self.link_parameter(self.tied_param)
        return labellist, idxlist

    def _remove_tie_param(self, labels):
        """Remove the tie param corresponding to *labels*"""
        if len(labels) == self.tied_param.size:
            self.unlink_parameter(self.tied_param)
            self.tied_param = None
        else:
            new_buf = np.empty((self.tied_param.size-len(labels),))
            idx = np.logical_not(np.in1d(self.tied_param.tie,labels))
            new_buf[:] = self.tied_param[idx]
            old_tie_ = self.tied_param.tie.copy()
            self.unlink_parameter(self.tied_param)            
            self.tied_param = Param('tied',new_buf)
            self.tied_param.tie[:] = old_tie_[idx]
            self.link_parameter(self.tied_param)
    
    def _merge_tie_labels(self, labels):
        """Merge all the labels in the list to the first one"""
        if len(labels)<2:
            return
        self._remove_tie_param(labels[1:])
        self._replace_labels(self._highest_parent_, [(l,labels[0]) for l in labels[1:]])

    def _merge_tie_labelpair(self, labelpair):
        """Merge the second list in labelpair to the first list"""
        self._remove_tie_param(labelpair[1])
        self._replace_labels(self._highest_parent_, zip(labelpair[1],labelpair[0]))
        
    def _remove_unnecessary_ties(self):
        """Remove the unnecessary ties"""
        if self.tied_param is not None:
            labels = [l for l in self.tied_param.tie if (self.label_buf==l).sum()<=2]
            if len(labels)>0:
                self._remove_tie_param(labels)
                self._replace_labels(self._highest_parent_, zip(labels,[0]*len(labels)))

    def _update_label_buf(self):
        if self.tied_param is None:
            self.label_buf = None
            self.buf_idx = None
            self._untie_ = None
        else:
            self.label_buf = np.zeros((self._highest_parent_.param_array.size,),dtype=np.uint32)
            self._traverse_param(lambda x:np.put(self.label_buf,self._highest_parent_._raveled_index_for(x),x.tie.flat), (self._highest_parent_,), [])
            self.buf_idx = self._highest_parent_._raveled_index_for(self.tied_param)
            self._untie_ = self.label_buf==0
            self._untie_[self.buf_idx] = True
            assert(np.all(self.tied_param.tie>0))
        
    def tie_together(self,plist):
        """tie a list of parameters"""
        self.update_model(False)
        labels = self._get_labels(plist)
        val = self._sync_val_group(plist)
        if labels[0]==0 and labels.size==1:
            # None of parameters in plist has been tied before.
            tie_labels,_ = self._expand_tie_param(1)
            self._set_labels(plist, tie_labels)
            tie_con = self._sync_constraint_group(plist)
            if tie_con is not None:
                self.tied_param[self.tied_param.tie==tie_labels[0]].constrain(tie_con)
        else:
            # Some of parameters has been tied already.
            # Merge the tie param
            tie_labels = labels[labels>0]
            if tie_labels.size>1:
                self._merge_tie_labels(tie_labels)
            self._set_labels(plist, [tie_labels[0]])
            tie_p = self.tied_param[self.tied_param.tie==tie_labels[0]]
            tie_con = tie_p.constraints.properties()[0] if tie_p.constraints.size>0 else None
            self._sync_constraint_group(plist, True, tie_con)
        self._update_label_buf()
        self.tied_param[self.tied_param.tie==tie_labels[0]] = val
        self.update_model(True)
        
    def tie_vector(self, p1, p2):
        """tie a pair of vectors"""
        self.update_model(False)        
        expandlist,removelist,labellist = self._get_labels_vector(p1, p2)
        p1vals = self._sync_val_pair(p1,p2)
        if len(expandlist)>0:
            tie_labels,idxlist = self._expand_tie_param(len(expandlist))
            labellist[expandlist] = tie_labels
            self.tied_param[idxlist] = p1vals[expandlist]
        if len(removelist[0])>0:
            self._merge_tie_labelpair(removelist)
        self._set_labels([p1,p2], labellist)
        self._sync_constraint_vector(p1,p2,expandlist,idxlist)
        self._update_label_buf()
        self.update_model(True)
        
    def untie(self,plist):
        """Untie a list of parameters"""
        self.update_model(False)
        self._set_labels(plist,[0])
        self._update_label_buf()
        self._remove_unnecessary_ties()
        self._update_label_buf()
        self.update_model(True)
        
    def _check_change(self):
        changed = False
        if self.tied_param is not None:
            for i in xrange(self.tied_param.size):
                b0 = self.label_buf==self.label_buf[self.buf_idx[i]]
                b = self._highest_parent_.param_array[b0]!=self.tied_param[i]
                if b.sum()==0:
                    # All the tied parameters are the same
                    continue
                elif b.sum()==1:
                    # One of the tied parameter is different.
                    # It must be recently changed one.
                    # The rest will be set to its value.
                    val = self._highest_parent_.param_array[b0][b][0]
                    self._highest_parent_.param_array[b0] = val
                else:
                    # It is most likely that the tie parameter is changed.
                    # Set all the tied parameter to the value of tie parameter.
                    self._highest_parent_.param_array[b0] = self.tied_param[i]
                changed = True
        return changed
    
    def _parameters_changed_notification(self, me, which=None):
        if which is not self:
            self._optimizer_copy_transformed = False # tells the optimizer array to update on next request
            self.parameters_changed()

    def parameters_changed(self):
        #ensure all out parameters have the correct value, as specified by our mapping
        if self._PROPAGATE_VAL_:
            self._PROPAGATE_VAL_ = False
        else:
            if self._check_change():
                self._highest_parent_._trigger_params_changed()
        self.collate_gradient()

    def collate_gradient(self):
        if self.tied_param is not None:
            self.tied_param.gradient = 0.
            [np.put(self.tied_param.gradient, i, self._highest_parent_.gradient[self.label_buf==self.label_buf[self.buf_idx[i]]].sum()) 
                for i in xrange(self.tied_param.size)]
    
    def propagate_val(self):
        if self.tied_param is not None:
            for i in xrange(self.tied_param.size):
                self._highest_parent_.param_array[self.label_buf==self.label_buf[self.buf_idx[i]]] = self.tied_param[i]
        self._PROPAGATE_VAL_ = True



