import numpy as np
from typing import List, Dict, Tuple, Set, Optional
from sentence_transformers import SentenceTransformer, util
import networkx as nx
import torch
#import torch.nn.functional as F
from dataclasses import dataclass
from collections import defaultdict
from dotenv import load_dotenv
import os, requests
import random
import streamlit as st
import json
from utils import tuples_to_list, generate_embeddings

load_dotenv()

groq_api_key = os.getenv('GROQ_API_KEY')

def find_cosine_similarity(embedding1, embedding2):
  global model
  # Encode the texts to get their embeddings
  #embedding1 = model.encode(text1, convert_to_tensor=True)
  #embedding2 = model.encode(text2, convert_to_tensor=True)
  
  # Compute cosine similarity
  cosine_sim = util.pytorch_cos_sim(embedding1, embedding2)
  
  # Print the cosine similarity score
  return cosine_sim.item()

def parse_response(raw_response):    
    # Example raw response as a string
    #raw_response = "[[\"entity1\", \"entity2\", \"entity3\"], [\"entity4\", \"entity5\", \"entity6\"]]"
    
    # Step 1: Parse the raw_response string into a Python list
    entity_sequences = json.loads(raw_response)
    
    # Step 2: Build a cause-effect map by creating a progression for each sequence
    cause_effect_map = []
    for sequence in entity_sequences:
        sequence_map = []
        for i in range(len(sequence) - 1):
            # Define a step-by-step cause-effect relationship
            cause = sequence[i]
            effect = sequence[i + 1]
            sequence_map.append(f"{cause} leads to {effect}")
        cause_effect_map.append(sequence_map)
    
    # Step 3: Display the cause-effect map
    parsed_response = ""
    start = 1
    for i, sequence_map in enumerate(cause_effect_map, start=1):
        print(f"Sequence {i}:")
        parsed_response += "\n" + f"Sequence {i} : "
        start = 1
        for step in sequence_map:
            if start == 0:
                print("  ->", step)
                parsed_response += f" -> {step}"
            else:
                print(step)
                parsed_response += step  
                start = 0
                
    return parsed_response

@dataclass
class Triple:
    """
    Represents a knowledge graph triple (head, relation, tail)
    Using dataclass for automatic implementation of __eq__, __hash__, etc.
    """
    head: str
    relation: str
    tail: str
    
    def __hash__(self):
        return hash((self.head, self.relation, self.tail))

class KnowledgeGraphRAG:
    def __init__(
        self,
        embedding_model: str = 'all-MiniLM-L6-v2',
        device: Optional[str] = None,
        seed: int = 42
        ):
        """
        Initialize RAG system with embedding model and empty knowledge graph
        
        Args:
            embedding_model: Name of the sentence-transformers model to use
            device: Device to run the model on ('cpu', 'cuda', etc.)
            seed: Random seed for reproducibility
        """
        # Set deterministic behavior across all libraries
        self._set_deterministic_settings(seed)

        # Determine device and initialize encoder
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        print(f"Using device: {self.device}")
        self.encoder = self._init_encoder(embedding_model)
        
        # Initialize graph and embedding storage
        self.knowledge_graph = nx.DiGraph()
        self.node_embeddings: Dict[str, torch.Tensor] = {}
        self.edge_embeddings: Dict[Tuple[str, str], torch.Tensor] = {}
        self.triple_to_edge: Dict[Triple, Tuple[str, str]] = {}

    def _set_deterministic_settings(self, seed: int) -> None:
        """
        Set all random seeds and ensure deterministic behavior
        Critical for reproducible results, especially on CPU
        """
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        
        # Enable deterministic operations
        torch.use_deterministic_algorithms(True)
        
        # Set CUDA settings if available
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
        
        # Set hash seed for consistent dictionary ordering
        os.environ['PYTHONHASHSEED'] = str(seed)
        
    def _init_encoder(self, model_name: str):
        """
        Initialize the encoder with deterministic settings
        Ensures consistent behavior across runs
        """
        # Wrap encoder.encode with no_grad for determinism
        with torch.no_grad():
            encoder = SentenceTransformer(model_name, device=self.device)
            
            # Freeze parameters for consistency
            for param in encoder.parameters():
                param.requires_grad = False
            
            return encoder

    def _compute_embedding(self, text: str) -> torch.Tensor:
        """
        Compute embedding with deterministic operations
        Ensures consistent embeddings across runs
        """
        with torch.no_grad():
            # Normalize text for consistent processing
            text = ' '.join(text.lower().split())
            
            # Compute embedding
            #embedding = self.encoder.encode(text, convert_to_tensor=True)
            embedding = generate_embeddings(text)
            # Ensure consistent numerical precision
            embedding = embedding.to(dtype=torch.float32)
            
            # Sort for consistent ordering
            #embedding = torch.sort(embedding)[0]
            
            return embedding.to(self.device)
        
    def add_triple(self, head: str, relation: str, tail: str) -> None:
        """
        Add knowledge triple to graph and compute embeddings
        
        Args:
            head: Source node of the triple
            relation: Relationship between head and tail
            tail: Target node of the triple
        """
        try:
            triple = Triple(head, relation, tail)
            
            # Add to graph with deterministic ordering
            self.knowledge_graph.add_edge(head, tail, relation=relation)
            
            # Compute node embeddings if not already present
            for node in sorted([head, tail]):  # Sort for consistency
                if node not in self.node_embeddings:
                    self.node_embeddings[node] = self._compute_embedding(node)
                    
            # Compute edge embedding
            edge_text = f"{head} {relation} {tail}"
            edge_key = (head, tail)
            self.edge_embeddings[edge_key] = self._compute_embedding(edge_text)
            self.triple_to_edge[triple] = edge_key
            
        except Exception as e:
            raise ValueError(f"Failed to add triple: {e}")
        
    def retrieve_relevant_subgraph(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.5
    ) -> List[Triple]:
        """
        Retrieve relevant subgraph with deterministic ordering
        
        Args:
            query: Input query text
            top_k: Number of top similar triples to return
            similarity_threshold: Minimum similarity score threshold
        """
        if not self.edge_embeddings:
            return []
            
        # Normalize query
        query = ' '.join(query.lower().split())

        print(f"Normalized query inside retrieve_relevant_subgraph : {query}")
        #Convert query to the formar {head} {relation} {tail} using chatgpt
        #parsed_query = parse_query(query)

        # Compute query embedding
        with torch.no_grad():
            query_embedding = self._compute_embedding(query)
        
        # Use ordered dictionary for consistent ordering
        from collections import OrderedDict
        similarities = OrderedDict()
        
        # Stack embeddings in deterministic order
        edge_keys = sorted(self.edge_embeddings.keys())
        #edge_embeddings_tensor = torch.stack([
        #    self.edge_embeddings[key] for key in edge_keys
        #])
        similarity_scores = []
        for key in edge_keys:
            similarity_scores.append(find_cosine_similarity(query_embedding, self.edge_embeddings[key]))

        # Compute similarities with fixed precision
        #with torch.no_grad():
        #    similarity_scores = F.cosine_similarity(
        #        query_embedding.unsqueeze(0),
        #        edge_embeddings_tensor
        #    )
        
        # Create deterministically ordered pairs
        print(f"DEBUG : Comparing against similarity threshold : {similarity_threshold}")
        max_score = -1
        max_score_triple = None
        for idx, (head, tail) in enumerate(edge_keys):
            score = similarity_scores[idx]
            #print(f"score is {score}")
            relation = self.knowledge_graph[head][tail]['relation']
            if score > max_score:
                max_score = score
                max_score_triple = Triple(head, relation, tail)
            if score >= similarity_threshold:
                #relation = self.knowledge_graph[head][tail]['relation']
                triple = Triple(head, relation, tail)
                #print(f"triple : {triple}")
                similarities[triple] = score
        
        # Sort by score and alphabetically for ties
        sorted_triples = sorted(
            similarities.items(),
            key=lambda x: (-x[1], x[0].head, x[0].relation, x[0].tail)
        )
        
        return [triple for triple, _ in sorted_triples[:top_k]], max_score, max_score_triple

    def expand_subgraph(
        self,
        triples: List[Triple],
        hops: int = 1,
        max_nodes_per_hop: int = 10
    ) -> List[Triple]:
        """
        Expand retrieved subgraph by following connections in a deterministic manner
        
        Args:
            triples: Initial set of triples to expand from
            hops: Number of hops to expand
            max_nodes_per_hop: Maximum number of neighbors to explore per hop
                
        Returns:
            List of expanded Triple objects in deterministic order
        """
        # Use sets for efficient membership testing
        expanded_triples: Set[Triple] = set(triples)
        seen_nodes: Set[str] = {node for triple in triples 
                            for node in (triple.head, triple.tail)}
        
        for _ in range(hops):
            new_triples: Set[Triple] = set()
            
            # Process triples in deterministic order
            for triple in sorted(expanded_triples, key=lambda x: (x.head, x.relation, x.tail)):
                # Process nodes in deterministic order
                for node in sorted([triple.head, triple.tail]):
                    # Get neighboring nodes in deterministic order
                    neighbors = sorted(list(self.knowledge_graph.neighbors(node)))
                    # Apply max_nodes_per_hop limit
                    neighbors = neighbors[:max_nodes_per_hop]
                    
                    # Process neighbors deterministically
                    for neighbor in neighbors:
                        if neighbor not in seen_nodes:
                            # Check outgoing edges
                            if self.knowledge_graph.has_edge(node, neighbor):
                                relation = self.knowledge_graph[node][neighbor]['relation']
                                new_triples.add(Triple(node, relation, neighbor))
                            
                            # Check incoming edges
                            if self.knowledge_graph.has_edge(neighbor, node):
                                relation = self.knowledge_graph[neighbor][node]['relation']
                                new_triples.add(Triple(neighbor, relation, node))
                            
                            seen_nodes.add(neighbor)
                
            expanded_triples.update(new_triples)
        
        # Return sorted list for deterministic ordering
        return sorted(list(expanded_triples), key=lambda x: (x.head, x.relation, x.tail))

    def generate_context(
        self,
        triples: List[Triple],
        format_type: str = 'natural'
    ) -> str:
        """
        Convert retrieved triples into context string with deterministic formatting
        
        Args:
            triples: List of triples to convert
            format_type: Output format ('natural' or 'structured')
                
        Returns:
            Formatted context string
        """
        # Sort triples for deterministic ordering
        sorted_triples = sorted(triples, key=lambda x: (x.head, x.relation, x.tail))
        
        if format_type == 'natural':
            # Create context strings in deterministic order
            context_strings = [
                f"{triple.head} {triple.relation} {triple.tail}."
                for triple in sorted_triples
            ]
            return " ".join(context_strings)
            
        elif format_type == 'structured':
            # Group by subject for organized output
            subject_groups = defaultdict(list)
            for triple in sorted_triples:
                subject_groups[triple.head].append((triple.relation, triple.tail))
            
            # Process groups in deterministic order
            context_parts = []
            for subject in sorted(subject_groups.keys()):
                # Sort predicates for deterministic ordering
                predicates = sorted(subject_groups[subject])
                predicate_str = "; ".join(
                    f"{rel} {obj}" for rel, obj in predicates
                )
                context_parts.append(f"{subject} -> {predicate_str}")
                
            return "\n".join(context_parts)
            
        else:
            raise ValueError(f"Unsupported format type: {format_type}")

def parse_query_with_groq(
    query: str,
    groq_api_key: str,
    seed: int = 42,
    llama_model: str = "llama-3.2-11b-text-preview"
) -> Optional[str]:
    """
    Enhanced query parsing with deterministic settings
    
    Args:
        query: Input query text
        groq_api_key: API key for Groq
        seed: Random seed for reproducibility
        llama_model: Model identifier
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    # Normalize query
    query = ' '.join(query.lower().split())
    
    headers = {
        "Authorization": f"Bearer {groq_api_key}",
        "Content-Type": "application/json"
    }
    
    # Enhanced system message for deterministic behavior
    system_message = """You are a deterministic analytical assistant.
    Process all inputs consistently using these rules:
    1. Always use the same formatting and structure
    2. Sort lists and elements alphabetically
    3. Use consistent terminology
    4. Maintain fixed decimal precision
    5. Follow a fixed reasoning pattern
    6. Avoid any randomization or variation in responses
    """
    
    payload = {
        "model": llama_model,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": query}
        ],
        "temperature": 0,  # Zero temperature for maximum determinism
        "top_p": 1,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "max_tokens": 500,
        "seed": seed,
        "stream": False
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        raw_response =  response.json()['choices'][0]['message']['content']
        parsed_response = parse_response(raw_response)
        return parsed_response
    except Exception as e:
        print(f"Error in API request: {e}")
        return None

def createQuery(graph: str, question: str) -> str:
    """
    Create a structured query with deterministic formatting
    
    Args:
        graph: Context information
        question: User question
    """
    # Normalize inputs
    graph = " ".join(graph.split())
    question = " ".join(question.split()).rstrip("?") + "?"
    
    # Enhanced prompt for deterministic responses
    query = f"""
        Context Information:
        {graph}

        Question: {question}

        Output Requirements:
        1. Format: Return a deterministically ordered list of lists
        2. Structure: [["entity1", "entity2", "entity3"], ["entity4", "entity5", "entity6"]]
        3. Rules:
            - *Inner List Size*: Each inner list must contain exactly between 3 to 5 entities. No inner list should have fewer than 3 or more than 5 items.
            - *Coherence Within Inner Lists*: Each entity within an inner list must logically lead to the next entity, forming a clear, step-by-step progression that builds a coherent sequence. Entity1 should naturally lead to entity2, which should lead to entity3, and so on. The entities should represent distinct yet connected ideas relevant to the question.
            - *Independence of Outer Lists*: Each outer list should represent a separate, self-contained line of reasoning or sequence of ideas related to the question, so that each list offers a distinct path for exploring the topic.
        4. Entity Guidelines:
            - Each entity should be concise and specific, using a short phrase that conveys a clear concept or idea directly tied to the question.
            - Avoid generic or vague terms; each entity should clearly reflect a step in the logical progression of the list.
            - *No Connecting Words Within Entities*: Refrain from using connectors like "because," "therefore," or "leads to." Each cause-effect relationship should be broken down into separate entities within the list.
        
        Return only the structured list without additional text.
    """
    return query

def demonstrate_rag(query, seed):
    """Example usage of the KnowledgeGraphRAG system"""
    try:
        # Normalize input query
        query = " ".join(query.split()).lower().rstrip("?") + "?"
        
        # Initialize system with fixed random seed
        random.seed(seed)
        np.random.seed(42)
        torch.manual_seed(42)
        os.environ['PYTHONHASHSEED'] = str(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(42)

        # Initialize system
        rag = KnowledgeGraphRAG()
        
        sample_triples = tuples_to_list('unique_output.txt')
        print(f"length of list of triples is {len(sample_triples)}")
        print(sample_triples[:-2])
        
        #Create a graph
        for head, relation, tail in sample_triples:
            rag.add_triple(head, relation, tail)
        
        # Retrieve and expand relevant triples
        relevant_triples, max_score, max_score_triple = rag.retrieve_relevant_subgraph(query, top_k=3, similarity_threshold=0.60)
        print(f"DEBUG : relevant_triples are {relevant_triples}")
        print(f"DEBUG : max_score {max_score} max_score_triple {max_score_triple}")
        if len(relevant_triples) > 0:
            expanded_triples = rag.expand_subgraph(relevant_triples, hops=1)
    
            # Sort triples for consistent output
            expanded_triples.sort(key=lambda x: (x.head, x.relation, x.tail))
    
            # Generate both natural and structured context
            natural_context = rag.generate_context(expanded_triples, format_type='natural')
            structured_context = rag.generate_context(expanded_triples, format_type='structured')
        else:
            structured_context = ""
            natural_context = ""
            
        return {
            'natural_context': natural_context,
            'structured_context': structured_context
        }
        
    except Exception as e:
        print(f"Error in demonstration: {e}")
        return None

def generate_analysis():
    #st.set_page_config(page_title="Link Logic", page_icon=":bar_chart:")
    st.title("Link Logic - Insights Simplified for the Time-Strapped Investor.")

    st.write("This application uses a Knowledge Graph Retrieval Augmented Generation (KG-RAG) system to provide information about how monsoon season affects Reliance's supply chain.")

    user_query = st.text_input("Enter your query:", placeholder="How does monsoon season affect Reliance's supply chain?")

    if st.button("Submit"):
        with st.spinner("Processing your query..."):
            results = demonstrate_rag(user_query, 42)
            print(f"DEBUG : results : {results}")
            if results["structured_context"] != "":
                #st.subheader("Natural Language Context:")
                #st.write(results['natural_context'])

                #st.subheader("Structured Context:")
                #st.write(results['structured_context'])

                query = createQuery(results['structured_context'], user_query)
                output = parse_query_with_groq(query, groq_api_key, 42)
                if output:
                    st.subheader("Response:")
                    st.text(output)
                else:
                    st.error("Unable to generate a response.")
            else:
                st.error("An error occurred while processing the query.")

if __name__ == "__main__":
    generate_analysis()
