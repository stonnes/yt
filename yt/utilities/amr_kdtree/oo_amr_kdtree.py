"""
AMR kD-Tree Framework

Authors: Samuel Skillman <samskillman@gmail.com>
Affiliation: University of Colorado at Boulder
Wil St. Charles <fallen751@gmail.com>
Affiliation: University of Colorado at Boulder

Homepage: http://yt.enzotools.org/
License:
  Copyright (C) 2010 Samuel Skillman.  All Rights Reserved.

  This file is part of yt.

  yt is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 3 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.

  You should have received a copy of the GNU General Public License
  along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import numpy as na
from yt.funcs import *
from yt.visualization.volume_rendering.grid_partitioner import HomogenizedVolume
from yt.utilities.amr_utils import PartitionedGrid
from yt.utilities.performance_counters import yt_counters, time_function
import yt.utilities.parallel_tools.parallel_analysis_interface as PT
from copy import deepcopy
from yt.config import ytcfg
import pdb
from time import time
import h5py
my_rank = ytcfg.getint("yt", "__parallel_rank")
nprocs = ytcfg.getint("yt", "__parallel_size")

def corner_bounds(split_dim, split, current_left = None, current_right = None):
    r"""
    Given a kd-Tree split dimension and position and bound to be
    modified, returns the new bound.

    A simple function that replaces the `split_dim` dimension of the
    current left or right bound with `split`. Left or Right bound is
    chosen by specifying the `current_left` or `current_right`.
    """
    if(current_left is not None):
        new_left = na.array([current_left[0],current_left[1],current_left[2]])
        new_left[split_dim] = split
        return new_left
    elif(current_right is not None):
        new_right = na.array([current_right[0],current_right[1],current_right[2]])
        new_right[split_dim] = split
        return new_right

def _lchild_id(id): return (id<<1) + 1
def _rchild_id(id): return (id<<1) + 2
def _parent_id(id): return (id-1)>>1

class MasterNode(object):
    r"""
    A MasterNode object is the building block of the AMR kd-Tree.
    Used during the construction to act as both dividing nodes and
    leaf nodes.
    """
    def __init__(self):
        self.grids = None
        self.parent = None
        self.parent_grid = None
        self.l_corner = None
        self.r_corner = None

        self.split_ax = None
        self.split_pos = None

        self.left_child = None
        self.right_child = None
        self.cost = 0
        # self.owner = -1

        self.id = None
        
        self.grid = None
        self.brick = None
        self.li = None
        self.ri = None
        self.dims = None

        # self.done = 0
        # self.cast_done = 0

def set_leaf(thisnode, grid_id, leaf_l_corner, leaf_r_corner):
    r"""
    Sets leaf properties.

    Parameters
    ----------
    thisnode : `MasterNode`
        AMR kd-Tree node to be modified.
    grid_id : `~yt.data_objects.grid_patch`
        A grid patch that contains the data spanned by this 
        kd-Tree leaf.
    leaf_l_corner: array_like, dimension 3
        The left corner of the volume spanned by this leaf.
    leaf_r_corner: array_like, dimension 3
        The right corner of the volume spanned by this leaf.
        
    Returns
    -------
    None
    """
    thisnode.grid = grid_id.id
    thisnode.l_corner = leaf_l_corner
    thisnode.r_corner = leaf_r_corner
    del thisnode.grids, thisnode.parent_grid, thisnode.split_ax, thisnode.split_pos

class AMRKDTree(HomogenizedVolume):
    def __init__(self, pf,  l_max=None, le=None, re=None,
                 fields=None, no_ghost=False,
                 tree_type='domain',log_fields=None):
        r"""
        AMR kd-Tree object, a homogenized volume.

        Definition of the AMR kd-Tree object.  This is a method of
        volume homogenization that uses a modified kd-Tree structure
        to partition the AMR hierarchy.  The dividing nodes of the
        tree subdivide the volume into left and right children along a
        particular dimension.  The leaf nodes of the tree contain
        subvolumes that are covered by a single single grid at a
        single resolution, usually the maximum level for that volume
        unless `l_max` is otherwise specified.  The volume can then be
        traversed along an arbitrary direction based on comparisions
        with the dividing node position and split dimenstion.  

        Parameters
        ----------
        pf : `~yt.data_objects.StaticOutput`
            The parameter file to be kd-Tree partitioned.
        l_max : int, optional
            Maximum level to use in construction of kd-Tree. Default:
            None (all levels)
        le: array_like, optional
            Left edge to be be partitioned. Default: None (Domain Left
            Edge)
        re: array_like. optional
            Right edge to be partitioned.  Default: None (Domain Right
            Edge)
        fields: list of strings, optional
            Fields to be obtained when collecting leaf data.  Defualt:
            None (['Density']).
        log_fields: list of bool, optional
            Specifies which fields are to be taken the logarithm of
            before rendering.
        no_ghost: bool, optional
            Optimization option.  If True, homogenized bricks will
            extrapolate out from grid instead of interpolating from
            ghost zones that have to first be calculated.  This can
            lead to large speed improvements, but at a loss of
            accuracy/smoothness in resulting image.  The effects are
            less notable when the transfer function is smooth and
            broad. Default: False
        tree_type: string, optional
            Specifies the type of kd-Tree to be constructed/cast.
            There are three options, the default being 'domain'. Only
            affects parallel rendering.  'domain' is suggested.

            'domain' - Tree construction/casting is load balanced by
            splitting up the domain into the first N subtrees among N
            processors (N must be a power of 2).  Casting then
            proceeds with each processor rendering their subvolume,
            and final image is composited on the root processor.  The
            kd-Tree is never combined, reducing communication and
            memory overhead. The viewpoint can be changed without
            communication or re-partitioning of the data, making it
            ideal for rotations/spins.

            'breadth' - kd-Tree is first constructed as in 'domain',
            but then combined among all the subtrees.  Rendering is
            then split among N processors (again a power of 2), based
            on the N most expensive branches of the tree.  As in
            'domain', viewpoint can be changed without re-partitioning
            or communication.

            'depth' - kd-Tree is first constructed as in 'domain', but
            then combined among all subtrees.  Rendering is then load
            balanced in a back-to-front manner, splitting up the cost
            as evenly as possible.  If the viewpoint changes,
            additional data might have to be partitioned.  Is also
            prone to longer data IO times.  If all the data can fit in
            memory on each cpu, this can be the fastest option for
            multiple ray casts on the same dataset.


        Returns
        -------
        An AMR kd-Tree of the static output, of type `AMRKDTree`  

        Examples
        --------
        These are written in doctest format, and should illustrate how to
        use the function.  Use the variables 'pf' for the parameter file, 'pc' for
        a plot collection, 'c' for a center, and 'L' for a vector. 

        >>> from yt.utilities.amr_kdtree import AMRKDTree
        >>> volume = AMRKDTree(pf)
        yt         DEBUG      2010-11-08 21:35:40,873 Initializing data storage.
        yt         DEBUG      2010-11-08 21:35:40,873 Counting grids.
        yt         DEBUG      2010-11-08 21:35:40,874 Your data uses the annoying hardcoded path.
        yt         DEBUG      2010-11-08 21:35:40,876 Detected packed HDF5
        yt         DEBUG      2010-11-08 21:35:40,876 Setting up classes.
        yt         DEBUG      2010-11-08 21:35:40,877 Counting grids.
        yt         DEBUG      2010-11-08 21:35:40,877 Allocating arrays for 801 grids
        yt         DEBUG      2010-11-08 21:35:40,877 Parsing hierarchy.
        yt         INFO       2010-11-08 21:35:40,877 Getting the binary hierarchy
        yt         INFO       2010-11-08 21:35:40,885 Finished with binary hierarchy reading
        yt         DEBUG      2010-11-08 21:35:40,886 Constructing grid objects.
        yt         DEBUG      2010-11-08 21:35:40,903 Initializing data grid data IO
        yt         DEBUG      2010-11-08 21:35:40,904 Detecting fields.
        yt         DEBUG      2010-11-08 21:35:40,904 Adding unknown detected fields
        yt         DEBUG      2010-11-08 21:35:40,905 Setting up derived fields
        yt         DEBUG      2010-11-08 21:35:40,999 Re-examining hierarchy
        yt         INFO       2010-11-08 21:35:41,000 Making kd tree from le [ 0.  0.  0.] to [ 1.  1.  1.]
        yt         INFO       2010-11-08 21:35:42,451 Total of 5720 leafs
        yt         INFO       2010-11-08 21:35:42,519 [0000] Nodes 11439
        yt         INFO       2010-11-08 21:35:42,520 [0000] Cost is 314219
        yt         INFO       2010-11-08 21:35:42,520 [0000] Volume is 1.000000e+00
        >>> volume.volume
        1.0
        >>> volume.total_cost
        314219
        >>> volume.tree[0]
        {'cast_done': 0,
        'cost': 314219,
        'done': 0,
        'grid': None,
        'l_corner': array([ 0.,  0.,  0.]),
        'owner': 0,
        'r_corner': array([ 1.,  1.,  1.]),
        'split_ax': 1,
        'split_pos': 0.5}

        """
        self.current_split_dim = 0

        self.pf = pf
        if nprocs > len(pf.h.grids):
            print('Parallel rendering requires that the number of \n \
            grids in the dataset is greater or equal to the number of \n \
            processors.  Reduce number of processors.')
            raise(KeyError)
        if fields is None: fields = ["Density"]
        self.no_ghost = no_ghost
        reduction_needed = {'domain':False,'depth':True,'breadth':True}
        self.tree_type = tree_type
        self.reduce_tree=reduction_needed[self.tree_type]
        self.bricks_loaded = False
        self.bricks = []
        self.brick_dimensions = []
        self.fields = ensure_list(fields)
        if log_fields is not None:
            log_fields = ensure_list(log_fields)
        else:
            log_fields = [self.pf.field_info[field].take_log
                         for field in self.fields]
        self.log_fields = log_fields

        if l_max is None:
            self.l_max = self.pf.hierarchy.max_level+1
        else:
            self.l_max = na.min([l_max,self.pf.hierarchy.max_level+1])

        if le is None:
            self.domain_left_edge = pf.domain_left_edge
        else:
            self.domain_left_edge = na.clip(na.array(le),pf.domain_left_edge, pf.domain_right_edge)
        if re is None:
            self.domain_right_edge = pf.domain_right_edge
        else:
            self.domain_right_edge = na.clip(na.array(re),pf.domain_left_edge, pf.domain_right_edge)

        self.my_l_corner = self.domain_left_edge
        self.my_r_corner = self.domain_right_edge

        mylog.info('Making kd tree from le %s to %s'% (self.domain_left_edge, self.domain_right_edge))
        root_grids = pf.hierarchy.get_levels().next()

        root_l_data = na.array([grid.LeftEdge for grid in root_grids])
        root_r_data = na.array([grid.RightEdge for grid in root_grids])
        root_we_want = na.all(root_l_data < self.my_r_corner,axis=1)*\
                       na.all(root_r_data > self.my_l_corner,axis=1)
        
        root_grids = root_grids[root_we_want]

        # Build the kd-Tree
        self.__build(root_grids, None, self.domain_left_edge, self.domain_right_edge)
                
        # Add properties to leafs/nodes
        self.total_cost = self.count_cost()
        # Calculate the total volume spanned by the tree
        self.volume = self.count_volume()
        mylog.info('[%04i] Cost is %d' % (my_rank,self.total_cost))
        mylog.info('[%04i] Volume is %e' % (my_rank,self.volume)) 

    def _overlap_check(self, le, re, brick, periodic=True):
        r"""Given a left and right edges along with a brick, tests overlap of any
        cells in the brick

        Parameters
        ----------
        le: array_like
            The left edge of the region being searched for overlap.
        re: array_like
            The right edge of the region being searched for overlap.
        periodic: boolean, optional
            Specifies whether search should include periodicity.  Default:True

        Returns
        ----------
        boolean: True if overlap is found, False otherwise.
        
        """
        if (le[0] < brick['r_corner'][0]) and (re[0] > brick['l_corner'][0]) and \
               (le[1] < brick['r_corner'][1]) and (re[1] > brick['l_corner'][1]) and \
               (le[2] < brick['r_corner'][2]) and (re[2] > brick['l_corner'][2]):
            return True

        if periodic:
            myle = deepcopy(le)
            myre = deepcopy(re)
            w = self.pf.domain_right_edge-self.pf.domain_left_edge
            for i in range(3):
                if myle[i] < self.pf.domain_left_edge[i]:
                    myle[i] += w[i]
                    myre[i] += w[i]
                if myre[i] > self.pf.domain_right_edge[i]:
                    myle[i] -= w[i]
                    myre[i] -= w[i]
                    
            if (myle[0] < brick['r_corner'][0]) and (myre[0] > brick['l_corner'][0]) and \
                   (myle[1] < brick['r_corner'][1]) and (myre[1] > brick['l_corner'][1]) and \
                   (myle[2] < brick['r_corner'][2]) and (myre[2] > brick['l_corner'][2]):
                return True
                
        return False

    def get_all_neighbor_bricks(self, brick_id, le=None, re=None, periodic=True, add_to_brick_dict=False):
        r"""Given a brick_id, finds all other bricks that share a face, edge, or
        vertex.  Alternatively, will find all neighbors to an
        arbitrary rectangular specified by left and right edges.

        Parameters
        ----------
        brick_id: int
            ID of the brick in question.
        le: array_like, optional
            The left edge of an arbitrarily specified rectangular solid
        re: array_like, optional
            The right edge of an arbitrarily specified rectangular solid
        periodic: boolean, optional
            Specifies whether search should include periodicity.  Default:True
        iterator: boolean, optional
            If true, will yield the brick ids instead of return a list

        Returns
        ----------
        neighbors: list
           A list of all neighbor brick ids.
        
        """
        if brick_id is not None:
            node = self.tree[brick_id]
        neighbors = []
        dx = self.pf.h.get_smallest_dx()
        if le is None:
            le = node['l_corner'] - dx
        if re is None:
            re = node['r_corner'] + dx

        nodes_to_check = [0]
        while len(nodes_to_check) > 0:
            thisnode = nodes_to_check.pop(0)
            if self.tree[thisnode]['grid'] is None:
                if self._overlap_check(le,re,self.tree[_lchild_id(thisnode)],periodic=periodic):
                    nodes_to_check.append(_lchild_id(thisnode))
                if self._overlap_check(le,re,self.tree[_rchild_id(thisnode)],periodic=periodic):
                    nodes_to_check.append(_rchild_id(thisnode))
            else:
                neighbors.append(thisnode)

        if add_to_brick_dict:
            self.tree[brick_id]['neighbor_bricks']=neighbors
        return neighbors

    def get_all_neighbor_grids(self, brick_id, le=None, re=None, periodic=True):
        r"""Given a brick_id, finds all other grids that share a face, edge, or
        vertex.  Alternatively, will find all neighbors to an
        arbitrary rectangular specified by left and right edges.

        Parameters
        ----------
        brick_id: int
            ID of the brick in question.
        le: array_like, optional
            The left edge of an arbitrarily specified rectangular solid
        re: array_like, optional
            The right edge of an arbitrarily specified rectangular solid
        periodic: boolean, optional
            Specifies whether search should include periodicity.  Default:True
        iterator: boolean, optional
            If true, will yield the grid ids instead of return a list

        Returns
        ----------
        neighbors: list
           A list of all neighbor grid ids.
        
        """
        grids = [self.tree[this_id]['grid'] for this_id in self.get_all_neighbor_bricks(
            brick_id, le=le, re=re, periodic=periodic)]
        return grids

    def locate_brick(self, position):
        r"""Given a position, find the brick that contains it.

        Parameters
        ----------
        pos: array_like
            Position being queried

        Returns
        ----------
        node_id: int
            Brick id that contains position.
        
        """
        node_id = 0
        while True:
            brick = self.tree[node_id]
            if brick['grid'] is not None:
                return node_id
            else:
                if position[brick['split_ax']] <= brick['split_pos']:
                    node_id = _lchild_id(node_id)
                else:
                    node_id = _rchild_id(node_id)

    def get_bricks(self):
        r"""Preload the bricks into the kd-Tree

        Traverses the tree, gets the vertex centered data, and
        attaches partitioned grids to the kd-Tree structure.
        
        Parameters
        ----------
        None

        Returns
        ----------
        None
        
        """
        if self.bricks_loaded: return
        current_saved_grids = []
        current_vcds = []

        for current_node in self.depth_traverse():
            if current_node.grid is not None:
                if current_node.grid in current_saved_grids:
                    dds = current_vcds[current_saved_grids.index(current_node.grid)]
                else:
                    dds = []
                    for i,field in enumerate(self.fields):
                        vcd = current_node.grid.get_vertex_centered_data(field,smoothed=True,no_ghost=self.no_ghost).astype('float64')
                        if self.log_fields[i]: vcd = na.log10(vcd)
                        dds.append(vcd)
                    current_saved_grids.append(current_node.grid)
                    current_vcds.append(dds)

                data = [d[current_node.li[0]:current_node.ri[0]+1,
                          current_node.li[1]:current_node.ri[1]+1,
                          current_node.li[2]:current_node.ri[2]+1].copy() for d in dds]

                current_node.brick = PartitionedGrid(current_node.grid.id, len(self.fields), data,
                                                        current_node.l_corner.copy(), 
                                                        current_node.r_corner.copy(), 
                                                        current_node.dims.astype('int64'))
                self.bricks.append(current_node.brick)
                self.brick_dimensions.append(current_node.dims)
        self.bricks = na.array(self.bricks)
        self.brick_dimensions = na.array(self.brick_dimensions)
        del current_saved_grids, current_vcds
        self.bricks_loaded = True
        
    def set_leaf_props(self,thisnode):
        r"""Given a leaf, gathers grid, indices, dimensions, and cost properties.

        Parameters
        ----------
        None

        Returns
        ----------
        None
        
        """
        thisnode.grid = self.pf.hierarchy.grids[thisnode.grid - 1]
        
        dds = thisnode.grid.dds
        gle = thisnode.grid.LeftEdge
        gre = thisnode.grid.RightEdge
        thisnode.li = ((thisnode.l_corner-gle)/dds).astype('int32')
        thisnode.ri = ((thisnode.r_corner-gle)/dds).astype('int32')
        thisnode.dims = (thisnode.ri - thisnode.li).astype('int32')
        # Here the cost is actually inversely proportional to 4**Level (empirical)
        thisnode.cost = (na.prod(thisnode.dims)/4.**thisnode.grid.Level).astype('int64')
        # Here is the old way
        # thisnode.cost = na.prod(thisnode.dims).astype('int64')
        

    def count_cost(self):
        r"""Counts the cost of the entire tree, while filling in branch costs.

        Parameters
        ----------
        None

        Returns
        ----------
        Total cost of rendering the kd-Tree

        At completion, each node in the kd-Tree carries the total cost
        of all branches and leaves it contains.
        
        """

        for node in self.depth_traverse():
            if node.grid is None:
                try:
                    node.cost = node.left_child.cost 
                except:
                    node.cost = 0
                try: node.cost += node.right_child.cost
                except:
                    pass
            else:
                self.set_leaf_props(node)
        return self.tree.cost

    def depth_traverse(self):
        current = self.tree
        previous = None
        while current is not None:
            yield current
            current, previous = self.step_depth(current, previous)

    def step_depth(self, current, previous):
        if current.grid is not None: # At a leaf, move back up
            previous = current
            # mylog.debug('moving up from leaf')
            current = current.parent
            
        elif current.parent is previous: # Moving down, go left first
            previous = current
            if current.left_child is not None:
                # mylog.debug('moving down to left child')
                current = current.left_child
            elif current.right_child is not None:
                # mylog.debug('no left, moving down to right child')
                current = current.right_child
            else:
                # mylog.debug('no left or right, moving to parent')
                current = current.parent
                
        elif current.left_child is previous: # Moving up from left, go right 
            previous = current
            if current.right_child is not None:
                # mylog.debug('moving down to right child')
                current = current.right_child
            else:
                # mylog.debug('no right, moving to parent')
                current = current.parent

        elif current.right_child is previous: # Moving up from right child, move up
            previous = current
            # mylog.debug('moving up from right child')
            current = current.parent
            
        return current, previous
    
    def viewpoint_traverse(self, viewpoint):
        current = self.tree
        previous = None
        mylog.debug('Starting with %s %s'%(current, previous))
        while current is not None:
            yield current
            current, previous = self.step_viewpoint(current, previous, viewpoint)

    def step_viewpoint(self, current, previous, viewpoint):
        #print current.grid
        #print current.split_ax, current.split_pos
        if current.grid is not None: # At a leaf, move back up
            previous = current
            current = current.parent
        elif current.split_ax is None: # This is a dead node
            previous = current
            current = current.parent

        elif current.parent is previous: # Moving down
            # mylog.debug(("%s, %s, %s, %s, %s, %s") %
            #             (current, previous, viewpoint, current.split_ax,
            #              current.split_pos,
            #              viewpoint[current.split_ax] < current.split_pos))
            previous = current
            if viewpoint[current.split_ax] < current.split_pos:
                if current.left_child is not None:
                    current = current.left_child
                else:
                    previous = current.left_child
            else:
                if current.right_child is not None:
                    current = current.right_child
                else:
                    previous = current.right_child
                
        elif current.left_child is previous: # Moving up from left 
            previous = current
            if viewpoint[current.split_ax] < current.split_pos:
                if current.right_child is not None:
                    current = current.right_child
                else:
                    current = current.parent
            else:
                current = current.parent
                    
        elif current.right_child is previous: # Moving up from right child
            previous = current
            if viewpoint[current.split_ax] > current.split_pos:
                if current.left_child is not None:
                    current = current.left_child
                else:
                    current = current.parent
            else:
                current = current.parent
        return current, previous
                
    def count_volume(self):
        r"""Calculates the volume of the kd-Tree

        Parameters
        ----------
        None

        Returns
        ----------
        Total volume of the tree.
        
        """
        v = 0.0
        for node in self.depth_traverse():
            if node.grid is not None:
                v += na.prod(node.r_corner - node.l_corner)
        return v

    def __build(self, grids, parent, l_corner, r_corner):
        r"""Builds the AMR kd-Tree

        Parameters
        ----------
        grids: array_like
            Array of grids that cover the volume to be decomposed into
            the kd-Tree
        parent: ~yt.data_objects.grid_patch
            The parent grid that covers the volume.  Can be None if
            the volume is not contained by a single grid.
        l_corner: array_like
            The left corner of the volume to be decomposed.
        r_corner: array_like
            The right corner of the volume to be decomposed
            
        Returns
        ----------
        An array of kd-Tree nodes that make up the AMR kd-Tree
        
        """
        self.tree = MasterNode()

        head_node = self.tree
        previous_node = None
        current_node = self.tree
        current_node.grids = grids
        current_node.l_corner = l_corner
        current_node.r_corner = r_corner
        # current_node.owner = my_rank
        current_node.id = 0
        par_tree_depth = long(na.log2(nprocs))

        while current_node is not None:
            try: ngrids = current_node.grids
            except:
                current_node, previous_node = self.step_depth(current_node, previous_node)
                continue

            if ((current_node.id + 1)>>par_tree_depth) == 1:
                # There are nprocs nodes that meet this criteria
                if (current_node.id+1-nprocs) is my_rank:
                    # I own this shared node
                    #current_node.owner = my_rank
                    self.my_l_corner = current_node.l_corner
                    self.my_r_corner = current_node.r_corner
                else:
                    current_node.owner = current_node.id+1-nprocs
                    current_node, previous_node = self.step_depth(current_node, previous_node)
                    continue
                
            # If we are in a single grid
            if len(current_node.grids) is 1:
                thisgrid = current_node.grids[0]
                # If we are in the specified domain
                if (thisgrid.LeftEdge[0] <= current_node.l_corner[0]) and (thisgrid.RightEdge[0] >= current_node.r_corner[0]) and \
                   (thisgrid.LeftEdge[1] <= current_node.l_corner[1]) and (thisgrid.RightEdge[1] >= current_node.r_corner[1]) and \
                   (thisgrid.LeftEdge[2] <= current_node.l_corner[2]) and (thisgrid.RightEdge[2] >= current_node.r_corner[2]):
                    # Check if we have children and have not exceeded l_max
                    if len(thisgrid.Children) > 0 and thisgrid.Level < self.l_max:
                        children = [child.id - 1 for child in thisgrid.Children  
                                    if na.all(child.LeftEdge < current_node.r_corner) & 
                                    na.all(child.RightEdge > current_node.l_corner)]

                        if len(children) > 0:
                            current_node.grids = self.pf.hierarchy.grids[na.array(children)]
                            current_node.parent_grid = thisgrid
                            # print 'My single grid covers the rest of the volume, and I have children, about to iterate on them'
                            del children
                            continue

                    set_leaf(current_node, thisgrid, current_node.l_corner, current_node.r_corner)
                    # print 'My single grid covers the rest of the volume, and I have no children'
                    current_node, previous_node = self.step_depth(current_node, previous_node)
                    continue

            # If we don't have any grids, this volume belongs to the parent        
            if len(current_node.grids) is 0:
                set_leaf(current_node, current_node.parent_grid, current_node.l_corner, current_node.r_corner)
                # print 'This volume does not have a child grid, so it belongs to my parent!'
                current_node, previous_node = self.step_depth(current_node, previous_node)
                continue

            self._build_dividing_node(current_node)
                
            current_node, previous_node = self.step_depth(current_node, previous_node)

    def _build_dividing_node(self, current_node):
        # Get the left edges for each child
        data = na.array([(child.LeftEdge, child.RightEdge) for child in current_node.grids])
        # Split along the best dimension
        best_choices = na.array([])
        best_dim = 0

        # This helps a lot in terms of speed, but in some crazy situation could cause problems.
        if data.shape[0] > 20:
            best_dim = na.argmax(current_node.r_corner - current_node.l_corner)
            best_choices = data[:,:,best_dim][(current_node.l_corner[best_dim] < data[:,:,best_dim]) &
                                               (data[:,:,best_dim] < current_node.r_corner[best_dim])]
            best_choices.sort()
        else:
            for d in range(3):
                choices = na.unique(data[:,:,d][(current_node.l_corner[d] < data[:,:,d]) &
                                                (data[:,:,d] < current_node.r_corner[d])])
                
                if choices.size > best_choices.size:
                    best_choices, best_dim = choices, d

        split = best_choices[(len(best_choices)-1)/2]

        less_ids = na.nonzero(data[:,0,best_dim] < split)[0]
        greater_ids = na.nonzero(split < data[:,1,best_dim])[0]
        
        current_node.split_ax = best_dim
        current_node.split_pos = split

        current_node.left_child = MasterNode()
        current_node.left_child.id = _lchild_id(current_node.id)
        current_node.left_child.parent = current_node
        current_node.left_child.parent_grid = current_node.parent_grid
        current_node.left_child.grids = current_node.grids[less_ids]
        current_node.left_child.l_corner = current_node.l_corner
        current_node.left_child.r_corner = corner_bounds(best_dim, split, current_right=current_node.r_corner)

        current_node.right_child = MasterNode()
        current_node.right_child.id = _rchild_id(current_node.id)
        current_node.right_child.parent = current_node
        current_node.right_child.parent_grid = current_node.parent_grid
        current_node.right_child.grids = current_node.grids[greater_ids]
        current_node.right_child.l_corner = corner_bounds(best_dim, split, current_left=current_node.l_corner)
        current_node.right_child.r_corner = current_node.r_corner

        del current_node.grids, current_node.parent_grid, current_node.brick, current_node.li, current_node.ri, current_node.dims

    def initialize_source(self):
        self.get_bricks()
        
    def traverse(self, back_center, front_center, image):
        r"""Traverses the kd-Tree, casting the partitioned grids from back to
            front.

        Given the back and front centers, and the image, ray-cast
        using the kd-Tree structure.

        Parameters
        ----------
        back_center: array_like
            Position of the back center from which to start moving forward.
        front_center: array_like
            Position of the front center to which the traversal progresses.
        image: na.array
            Image plane to contain resulting ray cast.
            
        Returns
        ----------
        None, but modifies the image array.
        
        See Also
        ----------
        yt.visualization.volume_rendering.camera
        
        """
        if self.tree is None: 
            print 'No KD Tree Exists'
            return
        self.image = image

        viewpoint = front_center

        for node in self.viewpoint_traverse(viewpoint):
            if node.grid is not None:
                yield node.brick
         
        self.reduce_tree_images(self.tree, front_center)
        self._barrier()
        
    def reduce_tree_images(self, tree, viewpoint, image=None):
        if image is not None:
            self.image = image
        rounds = int(na.log2(nprocs))

        my_node = tree
        my_node_id = 0
        my_node.owner = 0
        path = na.binary_repr(nprocs+my_rank)
        for i in range(rounds):
            my_node.left_child.owner = my_node.owner
            my_node.right_child.owner = my_node.owner + 2**(rounds-(i+1))
            if path[i+1] is '0': 
                my_node = my_node.left_child
                my_node_id = my_node.id
            else:
                my_node = my_node.right_child
                my_node_id = my_node.id
            
        for thisround in range(rounds,0,-1):
            print my_rank, 'my node', my_node_id
            parent = my_node.parent
            #print parent['split_ax'], parent['split_pos']
            if viewpoint[parent.split_ax] <= parent.split_pos:
                front = parent.left_child
                back = parent.right_child
            else:
                front = parent.right_child
                back = parent.left_child 

            mylog.debug('front owner %i back owner %i parent owner %i'%( front.owner, back.owner, parent.owner))
                
            # Send the images around
            if front.owner == my_rank:
                if front.owner == parent.owner:
                    mylog.debug( '%04i receiving image from %04i'%(my_rank,back.owner))
                    arr2 = PT._recv_array(back.owner, tag=back.owner).reshape(
                        (self.image.shape[0],self.image.shape[1],self.image.shape[2]))
                    for i in range(3):
                        # This is the new way: alpha corresponds to opacity of a given
                        # slice.  Previously it was ill-defined, but represented some
                        # measure of emissivity.
                        #print arr2.shape
                        #                ta = (1.0 - arr2[:,:,i+3])
                        ta = (1.0 - na.sum(self.image,axis=2))
                        ta[ta<0.0] = 0.0 
                        self.image[:,:,i  ] = self.image[:,:,i  ] + ta * arr2[:,:,i  ]
                else:
                    mylog.debug('Reducing image.  You have %i rounds to go in this binary tree' % thisround)
                    mylog.debug('%04i sending my image to %04i'%(my_rank,back.owner))
                    PT._send_array(self.image.ravel(), back.owner, tag=my_rank)

                
            if back.owner == my_rank:
                if front.owner == parent.owner:
                    mylog.debug('%04i sending my image to %04i'%(my_rank, front.owner))
                    PT._send_array(self.image.ravel(), front.owner, tag=my_rank)
                else:
                    mylog.debug('Reducing image.  You have %i rounds to go in this binary tree' % thisround)
                    mylog.debug('%04i receiving image from %04i'%(my_rank,front.owner))
                    arr2 = PT._recv_array(front.owner, tag=front.owner).reshape(
                        (self.image.shape[0],self.image.shape[1],self.image.shape[2]))
                    for i in range(3):
                        # This is the new way: alpha corresponds to opacity of a given
                        # slice.  Previously it was ill-defined, but represented some
                        # measure of emissivity.
                        # print arr2.shape
                        # ta = (1.0 - arr2[:,:,i+3])
                        ta = (1.0 - na.sum(arr2,axis=2))
                        ta[ta<0.0] = 0.0 
                        self.image[:,:,i  ] = arr2[:,:,i  ] + ta * self.image[:,:,i  ]
                        # image[:,:,i+3] = arr2[:,:,i+3] + ta * image[:,:,i+3]
            # Set parent owner to back owner
            # my_node = (my_node-1)>>1
            if my_rank == my_node.parent.owner: 
                my_node = my_node.parent
            else:
                break

    def store_kd_bricks(self, fn=None):
        if fn is None:
            fn = '%s_kd_bricks.h5'%self.pf
        if my_rank != 0:
            PT._recv_array(my_rank-1, tag=my_rank-1)
        f = h5py.File(fn,"a")
        for i, node in self.tree.iteritems():
            if 'brick' in node:
                for fi,field in enumerate(self.fields):
                    try:
                        f.create_dataset("/brick_%s_%s" % (hex(i),field),
                                         data = node['brick'].my_data[fi].astype('float64'))
                    except:
                        pass
        f.close()
        if my_rank != (nprocs-1):
            PT._send_array([0],my_rank+1, tag=my_rank)
        
    def load_kd_bricks(self,fn=None):
        if fn is None:
            fn = '%s_kd_bricks.h5' % self.pf
        if my_rank != 0:
            PT._recv_array(my_rank-1, tag=my_rank-1)
        try:
            f = h5py.File(fn,"r")
            for i, node in self.tree.iteritems():
                if node['grid'] is not None:
                        data = [f["brick_%s_%s" %
                                           (hex(i), field)][:].astype('float64') for field in self.fields]
                        node['brick'] = PartitionedGrid(node['grid'].id, len(self.fields), data,
                                                        node['l_corner'].copy(), 
                                                        node['r_corner'].copy(), 
                                                        node['dims'].astype('int64'))
            self.bricks_loaded=True
            f.close()
        except:
            pass
        if my_rank != (nprocs-1):
            PT._send_array([0],my_rank+1, tag=my_rank)

    def load_tree(self,fn):
        f = h5py.File(fn,"r")
        kd_ids = f["/kd_ids"][:]
        kd_l_corners = f['/left_edges'][:]
        kd_r_corners = f['/right_edges'][:]
        kd_grids = f['/grids'][:]
        kd_split_axs = f['/split_axs'][:]
        kd_split_pos = f['/split_pos'][:]
        kd_owners = f['/kd_owners'][:]

        mytree = {}
        for i,this_id in enumerate(kd_ids):
            mytree[this_id] = {'l_corner':kd_l_corners[i], 'r_corner':kd_r_corners[i],
                               'split_ax':kd_split_axs[i], 'split_pos':kd_split_pos[i], 'owner':kd_owners[i]}
            if kd_grids[i] == -1:
                mytree[this_id]['grid'] = None
                mytree[this_id]['brick'] = None
            else:
                mytree[this_id]['grid'] = kd_grids[i]
                mytree[this_id]['brick'] = [f["/bricks/brick_%s/%s" % (i, field)][:] for field in self.fields]
                mytree[this_id]['split_ax'] = None
                mytree[this_id]['split_pos'] = None
        f.close()

        self.tree = mytree


    def store_tree(self,fn):
        f = h5py.File(fn,"w")
        Nkd = len(self.tree)
        kd_l_corners = na.zeros( (Nkd, 3), dtype='float64')
        kd_r_corners = na.zeros( (Nkd, 3), dtype='float64')
        kd_grids = na.zeros( (Nkd) )
        kd_split_axs = na.zeros( (Nkd), dtype='int32')
        kd_split_pos = na.zeros( (Nkd), dtype='float64')
        kd_owners = na.zeros( (Nkd), dtype='int32')
        f.create_group("/bricks")
        for i, tree_item in enumerate(self.tree.iteritems()):
            kdid = tree_item[0]
            node = tree_item[1]
            kd_l_corners[i,:] = node['l_corner']
            kd_r_corners[i,:] = node['r_corner']
            if node['grid'] is None:
                kd_grids[i] = -1
                kd_split_axs[i] = node['split_ax']
                kd_split_pos[i] = node['split_pos']
            else:
                kd_grids[i] = node['grid'].id
                kd_split_axs[i] = -1
                kd_split_pos[i] = 0.0
            
            kd_owners[i] = node['owner']
            if 'brick' in node:
                f.create_group("/bricks/brick_%08i" % i)
                for fi,field in enumerate(self.fields):
                    f.create_dataset("/bricks/brick_%08i/%s" % (i,field),
                                     data = node['brick'].my_data[fi])
        f.create_dataset("/left_edges",data=kd_l_corners)
        f.create_dataset("/right_edges",data=kd_r_corners)
        f.create_dataset("/grids",data=kd_grids)
        f.create_dataset("/split_axs",data=kd_split_axs)
        f.create_dataset("/split_pos",data=kd_split_pos)
        f.create_dataset("/kd_owners",data=kd_owners)
        f.close()
