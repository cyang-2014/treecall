#!/usr/bin/env python2

# Author: Ni Huang <nihuang at genetics dot wustl dot edu>
# Author: Rachel Schwartz <Rachel dot Schwartz at asu dot edu>
# Author: Kael Dai <Kael dot Dai at asu dot edu>

from __future__ import print_function
import warnings
import signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

import sys
import itertools
import numpy as np
from scipy.stats import sem
from editdistance import eval as strdist
import vcf

with warnings.catch_warnings(ImportWarning):
    from ete2 import Tree

warnings.filterwarnings('error')


def read_vcf(filename, evidence=60):
    """Read vcf file - get info about variants

    Args:
        filename (str): vcf filename
        evidence (int): minimum evidence in Phred scale
            for a site to be considered, default 60

    Returns:
        vcf: a vcffile
        np.array (tuple): variant info (chrom, pos, ref)
            for each variant
        np.array (int): Number of high-quality bases observed
            for each of the 2 most common alleles for each variant
        np.array (int): List of Phred-scaled genotype likelihoods
            for each of the genotypes from the 2 most common alleles for each variant

    """
    vcffile = vcf.Reader(open(filename, 'r'))
    bases = ['A','C','G','T']
    variants,ADs,PLs = [],[],[]
    
    for v in vcffile:
        if v.ALT[0] in bases:
            variants.append((v.CHROM,v.POS,v.REF,v.ALT[0]))
            ad = [v.genotype(s).data.AD for s in vcffile.samples] #ad for each sample
            pl = [v.genotype(s).data.PL for s in vcffile.samples]
            ADs.append(ad)  
            PLs.append(pl) #PL's of three genotypes - ref/ref, ref/alt, alt/alt
    
    variants = np.array(variants)
    ADs = np.array(ADs, dtype=np.uint16)
    PLs = np.array(PLs, dtype=np.uint16)
    
    #for each variant, sum PL for each genotype across samples
    #genotypes are ordered from most to least likely NOT ref/ref, ref/alt, alt/alt
    #check if PL sums are all greater than evidence
    #this removes sites where the joint genotyping likelihood across samples
    #for second most likely genotype is < 10^-6
    #i.e. most likely genotype for each sample has strong support
    k_ev = (np.sort(PLs).sum(axis=1)>=evidence).sum(axis=1)==2  #this used to be ==3 but that doesn't seem right - it should be checked
    variants,ADs,PLs = variants[k_ev],ADs[k_ev],PLs[k_ev]

    print(' done', file=sys.stderr)
    return vcffile, variants, ADs, PLs


def neighbor_main(args):
    """generate neighbor-joining tree then do recursive NNI and recursive reroot

    Args:
        vcf(str): input vcf/vcf.gz file, "-" for stdin
        output(str): output basename
        mu (int): mutation rate in Phred scale, default 80
            WHY IS THE DEFAULT 80????? IE 10^-8!!!!!!!!!!!!!!!!
        het (int): heterozygous rate in Phred scale, default 30
        min_ev(int): minimum evidence in Phred scale for a site to be considered
            default 60
    
    Output:
        newick trees
    
    """
    print(args, file=sys.stderr)
    vcffile, variants, DPRs, PLs = read_vcf(args.vcf, args.min_ev)
    #variants =  np.array (tuple): variant info (chrom, pos, ref)  for each variant
    #DPRs = np.array (int): Number of high-quality bases observed for each of the 2 most common alleles for each variant
    #PLs = np.array (int): List of Phred-scaled genotype likelihoods for each of the 2 most common alleles (3 genotypes) for each variant
    
    GTYPE3 = np.array(('RR','RA','AA'))
    base_prior = make_base_prior(args.het, GTYPE3) # base genotype prior; heterozygous rate in Phred scale, default 30; e.g. for het=30 [ 3.0124709,  33.012471,  3.0124709]
    mm,mm0,mm1 = make_mut_matrix(args.mu, GTYPE3) # substitution rate matrix, with non-diagonal set to 0, with diagonal set to 0

    PLs = PLs.astype(np.longdouble)
    n_site,n_smpl,n_gtype = PLs.shape

    D = make_D(PLs)  # pairwise differences between samples based only on PLs (should include mutation, but also shouldn't matter)
    tree = init_star_tree(n_smpl)
    internals = np.arange(n_smpl)
    tree = neighbor_joining(D, tree, internals) #haven't checked this 

    tree = init_tree(tree) 
    tree = populate_tree_PL(tree, PLs, mm0, 'PL0')
    tree = calc_mut_likelihoods(tree, mm0, mm1)

    print(tree)
    tree.write(outfile=args.output+'.nj0.nwk', format=5)
    best_tree,best_PL = recursive_NNI(tree, mm0, mm1, base_prior)
    print(best_tree)
    best_tree,best_PL = recursive_reroot(best_tree, mm0, mm1, base_prior)
    print(best_tree)
    print('PL_per_site = %.4f' % (best_PL/n_site))
    best_tree.write(outfile=args.output+'.nj.nwk', format=5)


def init_star_tree(n):
    """Creates a tree, adds n children in star with numbers as names

    Args:
        n (int): Number of children in tree

    Returns:
        Tree: 
    """
    
    tree = Tree()
    for i in xrange(n):
        tree.add_child(name=str(i))
    return tree


def pairwise_diff(PLs, i, j):
    #these are likely not calculated quite correctly, but it might not matter esp if more NNI
    pli = normalize2d_PL(PLs[:,i])
    plj = normalize2d_PL(PLs[:,j])
    p = phred2p(pli+plj) # n x g
    return (1-p.sum(axis=1)).sum()  


def make_D(PLs):
    """
    Get pairwise differences between samples based only on PLs (e.g. for generating nj tree)
    
    Args:
        PLs (np.array (longdouble)): List of Phred-scaled genotype likelihoods
            for each of the 2 most common alleles for each variant
        
    Returns:
        np.array (longdouble)
    """
    n,m,g = PLs.shape   #n_site,n_smpl,n_gtype
    D = np.zeros(shape=(2*m-2,2*m-2), dtype=np.longdouble)
    for i,j in itertools.combinations(xrange(m),2):
        D[i,j] = pairwise_diff(PLs, i, j)
        D[j,i] = D[i,j]
    return D


def neighbor_joining(D, tree, internals):
    #fsum will have better precision when adding distances across sites
    #based on PLs not mutation
    """
    
    Args:
        D (np.array): pairwise differences between samples based only on PLs
        tree (Tree): tree of class Tree with num tips = num samples
        internals (np.array): array of sample numbers
        
    Returns:
        Tree
    
    """
    print('neighbor_joining() begin', end=' ', file=sys.stderr)
    m = len(internals)
    while m > 2:  #if m is 2 then only two connected to root
        d = D[internals[:,None],internals]
        u = d.sum(axis=1)/(m-2)

        Q = np.zeros(shape=(m,m), dtype=np.longdouble)
        for i,j in itertools.combinations(xrange(m),2):
            Q[i,j] = d[i,j]-u[i]-u[j]
            Q[j,i] = Q[i,j]
        #print(Q.astype(int))
        np.fill_diagonal(Q, np.inf)
        #print(np.unique(Q, return_counts=True))
        i,j = np.unravel_index(Q.argmin(), (m,m))
        l = len(D)+2-m

        for k in xrange(m):
            D[l,internals[k]] = D[internals[k],l] = d[i,k]+d[j,k]-d[i,j]
        D[l,internals[i]] = D[internals[i],l] = vi = (d[i,j]+u[i]-u[j])/2
        D[l,internals[j]] = D[internals[j],l] = vj = (d[i,j]+u[j]-u[i])/2

        ci = tree&str(internals[i])
        cj = tree&str(internals[j])
        ci.detach()
        cj.detach()
        node = Tree(name=str(l))
        node.add_child(ci,dist=int(vi))
        node.add_child(cj,dist=int(vj))
        tree.add_child(node)
        #print(tree)

        internals = np.delete(internals, [i,j])
        internals = np.append(internals, l)
        m = len(internals)
        print('.', end='', file=sys.stderr)

    print(' done', file=sys.stderr)
    return tree

def init_tree(tree):
    """
    node.sid = list of children

    """
    tree.leaf_order = map(int, tree.get_leaf_names())

    for node in tree.traverse(strategy='postorder'):
        if node.is_leaf():
            node.sid = [int(node.name)]
        else:
            node.name = ''
            node.sid = []
            for child in node.children:
                node.sid.extend(child.sid)

    m = len(tree)
    for i,node in zip(xrange(2*m-1), tree.traverse(strategy='postorder')):
        node.nid = i
        node.sid = sorted(node.sid)
        
    return tree


def p2phred(x):
    return -10.0*np.log10(x)

def phred2p(x):
    return 10.0**(-x/10.0)

def sum_PL(x, axis=None):
    return p2phred(phred2p(x).sum(axis=axis))

def normalize_PL(x):
    p = 10.0**(-x/10.0)
    return -10.0*np.log10(p/p.sum())

def normalize2d_PL(x):
    p = 10.0**(-x/10.0)
    return -10.0*np.log10(p/p.sum(axis=1)[:,None])


def gtype_distance(gt):
    """
    Args:
        gt(np.array (str)): genotypes as 1d array - usually either GTYPE3 (generic het/homos) or GTYPE10 (all possible gtypes)
    
    Return:
        np.array(int): Levenshtein (string) distance between pairs - eg AA-RR = 2
    """ 
    n = len(gt)
    gt_dist = np.zeros((n,n), dtype=int)
    for i,gi in enumerate(gt):
        for j,gj in enumerate(gt):
            gt_dist[i,j] = min(int(strdist(gi,gj)),int(strdist(gi,gj[::-1])))
            
    return gt_dist


def make_mut_matrix(mu, gtypes):
    """Makes a matrix for genotypes - only depends on mu
    
    Args:
        mu (int): mutation rate in Phred scale, default 80
        gtypes(np.array (str)): genotypes as 1d array - usually either GTYPE3 (generic het/homos) or GTYPE10 (all possible gtypes)
        
    Returns:
        np.array(float): substitution rate matrix
        np.array(float): substitution rate matrix with non-diagonal set to 0
        np.array(float): substitution rate matrix with diagonal set to 0
    """
    pmu = phred2p(mu)  #80 -> 10e-08
    gt_dist = gtype_distance(gtypes) #np.array: Levenshtein (string) distance between pairs - eg AA-RR = 2
    mm = pmu**gt_dist
    np.fill_diagonal(mm, 2.0-mm.sum(axis=0))
    mm0 = np.diagflat(mm.diagonal()) # substitution rate matrix with non-diagonal set to 0
    mm1 = mm - mm0 # substitution rate matrix with diagonal set to 0
    
    return mm,mm0,mm1


def make_base_prior(het, gtypes):
    """Base prior probs
    for het=30, GTYPE3 = np.array(('RR','RA','AA'))
        [ 3.0124709,  33.012471,  3.0124709]

    for het=30, GTYPE10 = np.array(('AA','AC','AG','AT','CC','CG','CT','GG','GT','TT'))
        [ 6.0271094, 36.027109, 36.027109, 36.027109, 6.0271094, 36.027109, 36.027109, 6.0271094, 36.027109, 6.0271094]
    
    Args:
        het (int): heterozygous rate in Phred scale, default 30
        gtypes(np.array (str)): genotypes as 1d array
    
    Returns:
        np.array
    
    """
    return normalize_PL(np.array([g[0]!=g[1] for g in gtypes], dtype=np.longdouble)*het)


def calc_mut_likelihoods(tree, mm0, mm1):
    n,g = tree.PL0.shape
    for node in tree.traverse(strategy='postorder'):
        if not node.is_leaf():
            node.PLm = np.zeros((2*len(node)-2,n,g), dtype=np.longdouble)

    for node in tree.traverse(strategy='postorder'):
        i = 0
        for child in node.children:
            sister = child.get_sisters()[0]
            if not child.is_leaf():
                l = child.PLm.shape[0]
                node.PLm[i:(i+l)] = p2phred(np.dot(phred2p(child.PLm), mm0)) + p2phred(np.dot(phred2p(sister.PL0), mm0))
                i += l
            node.PLm[i] = p2phred(np.dot(phred2p(child.PL0), mm1)) + p2phred(np.dot(phred2p(sister.PL0), mm0))
            i += 1

    return tree

def update_PL(node, mm0, mm1):
    """
    PL for nodes depend on children so must be updated if node children change due to nni/reroot
    """
    #fix this so it returns something and doesn't try to use a global variable
    n,g = node.PL0.shape
    l = 2*len(node)-2
    #node.PL0 = np.zeros((n,g), dtype=np.longdouble)
    node.PL0.fill(0.0)
    node.PLm = np.zeros((l,n,g), dtype=np.longdouble)
    for child in node.children:
        sid = sorted(map(int,child.get_leaf_names()))
        if child.sid != sid:
            update_PL(child, mm0, mm1)
            child.sid = sid
        node.PL0 += p2phred(np.dot(phred2p(child.PL0), mm0)) 
    i = 0
    for child in node.children:
        sister = child.get_sisters()[0]
        if not child.is_leaf():
            l = child.PLm.shape[0]
            node.PLm[i:(i+l)] = p2phred(np.dot(phred2p(child.PLm), mm0)) + p2phred(np.dot(phred2p(sister.PL0), mm0))
            i += l
        node.PLm[i] = p2phred(np.dot(phred2p(child.PL0), mm1)) + p2phred(np.dot(phred2p(sister.PL0), mm0)) 
        i += 1


def populate_tree_PL(tree, PLs, mm, attr): #e.g. populate_tree_PL(tree, PLs, mm0, 'PL0')
    """
    
    Args:
        tree (Tree)
        PLs (np.array): phred scaled likelihoods
        mm: mutation matrix (np array of float) (mm0 has non-diagonal set to 0; mm1 has diagonal set to 0)
        attr: attribute to be set e.g. PL0
    
    Returns:
        Tree: now has PLs attached to nodes
    """
    n,m,g = PLs.shape # n sites, m samples, g gtypes
    for node in tree.traverse(strategy='postorder'):
        if node.is_leaf():
            setattr(node, attr, PLs[:,node.sid[0],])  #sid is list of children's labels (numbers) - using 0 b/c only one label for leaf
        else:
            setattr(node, attr, np.zeros((n,g), dtype=np.longdouble))
            for child in node.children:
                setattr(node, attr, getattr(node, attr) + p2phred(np.dot(phred2p(getattr(child, attr)), mm))) #sum of phred of each child's likelihoods*mut matrix
                
    return tree

def score(tree, base_prior):
    """
    used to compare rootings of tree
    
    """
    Pm = phred2p(tree.PLm+base_prior).sum(axis=(0,2))       #why add baseprior
    P0 = phred2p(tree.PL0+base_prior).sum(axis=1)
    return p2phred(Pm+P0).sum()


def annotate_nodes(tree, attr, values):
    #fix this so it returns something and doesn't try to use a global variable
    for node in tree.iter_descendants('postorder'):
        setattr(node, attr, values[node.nid])

    return tree

def read_label(filename):
    """from tab delim file: dict of
        key: first col in file / index
        value: second col in file (or first col if only one)
    """
    label = {}
    with open(filename) as f:
        i = 0
        for line in f:
            c = line.rstrip().split('\t')
            if len(c) > 1:
                label[c[0]] = c[1]
            else:
                label[str(i)] = c[0]
            i += 1
    return label

def partition(PLs, tree, sidx, min_ev):
    if tree.is_root():
        print('partition() begin', end=' ', file=sys.stderr)
    m = len(sidx) # number of samples under current node
    print(m, end='.', file=sys.stderr)
    if m == 2:
        child1 = tree.add_child(name=str(sidx[0]))
        child1.add_features(samples=np.atleast_1d(sidx[0]))
        child2 = tree.add_child(name=str(sidx[1]))
        child2.add_features(samples=np.atleast_1d(sidx[1]))
    elif m > 2:
        smat = make_selection_matrix2(m)
        pt, cost = calc_minimum_pt_cost(PLs, smat, min_ev)
        k0 = pt==0
        sidx0 = np.atleast_1d(sidx[k0])
        child = tree.add_child(name=','.join(sidx0.astype(str)))
        child.add_features(samples=sidx0)
        if len(sidx0) > 1:
            partition(PLs[:,k0,], child, sidx0, min_ev)
        k1 = pt==1
        sidx1 = np.atleast_1d(sidx[k1])
        child = tree.add_child(name=','.join(sidx1.astype(str)))
        child.add_features(samples=sidx1)
        if len(sidx1) > 1:
            partition(PLs[:,k1,], child, sidx1, min_ev)
    else:
        print('m<=1: shouldn\'t reach here', file=sys.stderr)
        sys.exit(1)
    if tree.is_root():
        print(' done', file=sys.stderr)


def calc_minimum_pt_cost(PLs, smat, min_ev):
    n,m,g = PLs.shape
    pt_cost = np.inf
    for k in smat:
        x0 = PLs[:,k==0,].sum(axis=1) # dim = n_site x 2
        x0min = x0.min(axis=1) # dim = n_site x 1
        x0max = x0.max(axis=1) # dim = n_site x 1
        x1 = PLs[:,k==1,].sum(axis=1) # dim = n_site x 2
        x1min = x1.min(axis=1) # dim = n_site x 1
        x1max = x1.max(axis=1) # dim = n_site x 1
        # take everything
        #c = (x0 + x1).sum()
        # cap the penalty by mu
        #c = (x0>mu).sum()*mu + x0[x0<=mu].sum() + (x1>mu).sum()*mu + x1[x1<=mu].sum()
        # ignore sites where signal from either partitions is weak
        #c = (x0min+x1min)[(x0max>min_ev) & (x1max>min_ev)].sum()
        # ignore sites where signals from both partitions are weak
        c = (x0min+x1min)[(x0max>min_ev) | (x1max>min_ev)].sum()
        # some weird cost function that broadly penalize partition of similar samples
        #k0 = x0.argmin(axis=1)
        #k1 = x1.argmin(axis=1)
        #c = np.minimum(x0[k0],x1[k1]).sum() + (k0==k1).sum()*mu
        if c < pt_cost:
            pt_cost = c
            pt = k
    return pt, pt_cost


def make_selection_matrix(m, t=20):
    n = 2**(m-1)
    if m>3 and m<=t: # special treatment for intermediate size
        l = (m,)*n
        x = np.array(map(tuple, map(str.zfill, [b[2:] for b in map(bin, xrange(4))], (3,)*4)), dtype=np.byte)
        y = np.zeros((n,m),dtype=np.byte)
        for i in xrange(m-3):
            a,b = x.shape
            y[0:a,-b:] = x
            y[a:(2*a),-b:] = x
            y[a:(2*a),-b] = 1
            x = y[0:(2*a),-(b+1):]
        for s in y:
            yield s
    else:
        for i in xrange(n):
            yield np.array(tuple(bin(i)[2:].zfill(m)), dtype=np.byte)


def make_selection_matrix2(m, t=20):
    n = 2**(m-1)
    if m>3 and m<=t: # special treatment for intermediate size
        l = (m,)*n
        x = np.array(map(tuple, map(str.zfill, [b[2:] for b in map(bin, xrange(4))], (3,)*4)), dtype=np.byte)
        y = np.zeros((n,m),dtype=np.byte)
        for i in xrange(m-3):
            a,b = x.shape
            y[0:a,-b:] = x
            y[a:(2*a),-b:] = x
            y[a:(2*a),-b] = 1
            x = y[0:(2*a),-(b+1):]
        for s in y:
            yield s
    elif m<=3:
        for i in xrange(n):
            yield np.array(tuple(bin(i)[2:].zfill(m)), dtype=np.byte)
    else:
        r1 = np.random.randint(1,m-1,2**t)
        r2 = np.random.rand(2**t)
        x = ((1+r2)*2**r1).astype(int)
        for i in iter(x):
            yield np.array(tuple(bin(i)[2:].zfill(m)), dtype=np.byte)


def reroot(tree, mm0, mm1, base_prior,DELTA):
    """
    
    return:
        Tree
        np.array (PLs)
        int: flag if rerooted (1) or not (0)
    """
    '''
              /-A              /-A              /-B
           /-|              /-|              /-|
    -root-|   \-B => -root-|   \-C => -root-|   \-C
          |                |                |
           \-C              \-B              \-A
    '''

    best_tree = tree
    best_PL = score(tree, base_prior)
    flag = 0

    for node in tree.iter_descendants('postorder'):
        tree_reroot = tree.copy()
        new_root = tree_reroot.search_nodes(sid=node.sid)[0]  #gets node of interest
        tree_reroot.set_outgroup(new_root)  #sets node of interest to outgroup
        update_PL(tree_reroot, mm0, mm1)  #new PL given decendants
        PL_reroot = score(tree_reroot, base_prior) 
        #print(tree_reroot)
        #print(PL_reroot)
        if PL_reroot < best_PL * (1-DELTA): #new best tree only if significantly better ie trees could be similar but status quo wins
            best_tree = tree_reroot
            best_PL = PL_reroot
            flag = 1
            
    return best_tree,best_PL,flag


def recursive_reroot(tree, mm0, mm1, base_prior,DELTA):
    """
    starting at tips, work up tree, get best way of rooting subtree (3 possibilities per, not whole subtree)
    """
    print('recursive_reroot() begin', end=' ', file=sys.stderr)
    for node in tree.iter_descendants('postorder'):
        if node.is_leaf():
            continue
        print('.', end='', file=sys.stderr)
        new_node,new_PL = reroot(node,mm0,mm1,base_prior,DELTA)  #checks if better way to root subtree
        parent = node.up
        parent.remove_child(node)
        parent.add_child(new_node)  #regrafts subtree w new root
        update_PL(tree, mm0, mm1)
    new_tree,new_PL = reroot(tree, mm0, mm1, base_prior,DELTA)  #check if better way to root whole tree assuming subtrees
    print(' done', end='', file=sys.stderr)
    #print(new_tree)
    #print(new_PL)
    
    return new_tree,new_PL


def nearest_neighbor_interchange(node, mm0, mm1, base_prior,DELTA):
    '''
    
    Return:
        node
        np.array(PL)
        int: flag to indicate nni happened
    
    
              /-A              /-A              /-A
           /-|              /-|              /-|
          |   \-B          |   \-C          |   \-D
    -node-|       => -node-|       => -node-|
          |   /-C          |   /-B          |   /-B
           \-|              \-|              \-|
              \-D              \-D              \-C
           ||               ||               ||
           \/               \/               \/
        reroot()         reroot()         reroot()
    '''
    
    flag = 0  #indicates rerooting
    c1,c2 = node.children
    
    #children are leaves - don't need to swap anything
    if c1.is_leaf() and c2.is_leaf():
        return None,None,0
    
    #one child is a leaf - rerooting will provide all possible combinations - flagged if rerooted
    if c1.is_leaf() or c2.is_leaf():
        return reroot(node, mm0, mm1, base_prior)

    #current arrangement (1st tree) - don't swap just reroot
    node_copy0 = node.copy()
    node0,PL0,flag = reroot(node_copy0, mm0, mm1, base_prior)
    
    #2nd tree - swap relationships and reroot
    node_copy1 = node.copy()
    c1,c2 = node_copy1.children
    c11,c12 = c1.children
    c21,c22 = c2.children
    c12 = c12.detach()
    c22 = c22.detach()
    c1.add_child(c22)
    c2.add_child(c12)
    update_PL(node_copy1, mm0, mm1)
    node1,PL1,flag = reroot(node_copy1, mm0, mm1, base_prior)
    
    #3rd tree - swap relationships and reroot
    node_copy2 = node.copy()
    c1,c2 = node_copy2.children
    c11,c12 = c1.children
    c21,c22 = c2.children
    c12 = c12.detach()
    c21 = c21.detach()
    c1.add_child(c21)
    c2.add_child(c12)
    update_PL(node_copy2, mm0, mm1)
    node2,PL2,flag = reroot(node_copy2, mm0, mm1, base_prior)

    if PL1 < PL0 * (1-DELTA):
        if PL1 < PL2:
            return node1,PL1,1  #return flag 1 if not original tree
        else:
            return node2,PL2,1
    if PL2 < PL0 * (1-DELTA):
        return node2,PL2,1
    else:
        return node0,PL0,flag  #flag depends on whether rerooting required


def recursive_NNI(tree, mm0, mm1, base_prior,DELTA):
    #recursive just means traverse the tree 
    """
    
    Args:
        tree(Tree)
        mm0: mutation matrix (np array of float) (non-diagonal set to 0)
        mm1: mutation matrix (np array of float) (diagonal set to 0)
        base_prior (np.array): Base prior probs depending on het pl

    Returns:
        Tree (tree)
        np.array (PL): phred-scaled likelihooods 
    
    """
    print('recursive_NNI() begin', end=' ', file=sys.stderr)
    #goes until can get through tree w/o nni at any node
    #a la phylip
    num_nnis=1
    while(num_nnis>0):
        num_nnis=0
        for node in tree.traverse('postorder'):
            #goes through each node, does nni if better
            if node.is_leaf():
                continue
            print('.', end='', file=sys.stderr)
            node_nni,PL_nni,nniflag = nearest_neighbor_interchange(node, mm0, mm1, base_prior,DELTA)
            if node_nni is None:
                continue
            if node.is_root():
                tree = node_nni
                PL = PL_nni
            else:
                parent = node.up
                node.detach()
                parent.add_child(node_nni)
                update_PL(tree, mm0, mm1)
                PL = score(tree, base_prior)
            if nniflag==1:
                num_nnis+=1
    print(' done', file=sys.stderr)
    #print(tree)
    #print(PL)
    return tree,PL